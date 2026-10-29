"""The retry loop: Copywriter -> Checker, and back for the posts the Checker rejects.

    checked = await write_and_check(key_moments, agents_for(film, settings, client, meter), meter)

Every post in the first draft goes to the Checker. A post the Checker passes is
frozen. A rejected post goes back to the Copywriter, alone, with reasons built only
from the words in the post itself (never the Spoiler List or the Checker's own
words, ADR 0007), and its rewrite is traced and checked again: at most MAX_RETRIES
times. After that it is flagged for a person, with the Checker's reasons.

It fails closed. A post is "passed" only when the Checker passed that exact text;
a Checker or Copywriter that gives up leaves its posts flagged, never passed. The
Film Budget stops the loop like any other call.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from trailer_to_campaign.agents import load_prompt
from trailer_to_campaign.agents.checker import Verdict
from trailer_to_campaign.agents.copywriter import Rewrite
from trailer_to_campaign.agents.key_moments import KeyMoment
from trailer_to_campaign.agents.loop import AgentFailed
from trailer_to_campaign.agents.social_posts import ALL_PAIRS, SocialPost
from trailer_to_campaign.budget import FilmMeter
from trailer_to_campaign.config import Settings

MAX_RETRIES = 2

Pair = tuple[str, str]


@dataclass(frozen=True)
class CheckedPost:
    post: SocialPost
    status: str  # "passed" (the Checker passed this exact text) or "flagged" (a person must look)
    problems: list[str]  # for the marketer: may describe the spoiler, so never shown to the Copywriter
    retries: int


@dataclass(frozen=True)
class Agents:
    write: Callable[[list[KeyMoment]], Awaitable[list[SocialPost]]]
    revise: Callable[[list[KeyMoment], list[Rewrite]], Awaitable[list[SocialPost]]]
    check: Callable[[list[SocialPost]], Awaitable[list[Verdict]]]


def agents_for(film: str, settings: Settings, client: Any, meter: FilmMeter) -> Agents:
    """The real Copywriter and Checker, each on its own servers (ADR 0007)."""
    from trailer_to_campaign.agents import checker, copywriter

    def write(key_moments: list[KeyMoment]) -> Awaitable[list[SocialPost]]:
        servers = copywriter.copywriter_servers(settings)
        return copywriter.write_posts(film, key_moments, servers=servers, client=client, meter=meter)

    def revise(key_moments: list[KeyMoment], rewrites: list[Rewrite]) -> Awaitable[list[SocialPost]]:
        servers = copywriter.copywriter_servers(settings)
        return copywriter.revise_posts(film, key_moments, rewrites, servers=servers, client=client, meter=meter)

    def check(posts: list[SocialPost]) -> Awaitable[list[Verdict]]:
        return checker.check_posts(film, posts, servers=checker.checker_servers(settings), client=client, meter=meter)

    return Agents(write=write, revise=revise, check=check)


def _pair(post: SocialPost) -> Pair:
    return (post.platform, post.language)


def for_the_writer(verdict: Verdict) -> list[str]:
    """Why a post was rejected, told only in the post's own words: no spoiler id, text or Checker reason."""
    reasons = []
    for p in verdict.problems:
        if p.kind == "spoiler":
            reasons.append(load_prompt("rewrite_reason_spoiler").format(words=repr(p.offending_text)))
        else:
            reasons.append(load_prompt("rewrite_reason_brand").format(words=repr(p.offending_text), rule=p.rule))
    return reasons


def for_the_marketer(verdict: Verdict) -> list[str]:
    """Why a post was rejected, in full, for the person who decides at Approval."""
    return [
        f"{'spoiler ' + str(p.spoiler_id) if p.kind == 'spoiler' else 'brand (' + str(p.rule) + ')'}: "
        f"{p.reason} (the words {p.offending_text!r})"
        for p in verdict.problems
    ]


async def write_and_check(
    key_moments: list[KeyMoment], agents: Agents, meter: FilmMeter, first_draft: list[SocialPost] | None = None
) -> list[CheckedPost]:
    """The checked posts, in platform and language order.

    first_draft replaces the Copywriter's first draft (the Eval Set plants Traps this
    way); it must already be traced, and it is checked like any other draft.
    """
    pending = first_draft if first_draft is not None else await agents.write(key_moments)
    drafted = {_pair(p) for p in pending}
    done: dict[Pair, CheckedPost] = {}
    problems_so_far: dict[Pair, list[str]] = {}

    for retry in range(MAX_RETRIES + 1):
        try:
            verdicts = await agents.check(pending)
        except AgentFailed as err:
            for post in pending:
                why = [*problems_so_far.get(_pair(post), []), f"the Checker could not review this post: {err}"]
                done[_pair(post)] = _flag(meter, post, why, retry)
            break

        rejected = []
        for post, verdict in zip(pending, verdicts, strict=True):
            if verdict.passed:
                done[_pair(post)] = CheckedPost(post, "passed", [], retry)
            else:
                problems_so_far[_pair(post)] = for_the_marketer(verdict)
                rejected.append((post, verdict))
        if not rejected:
            break
        if retry == MAX_RETRIES:
            for post, _ in rejected:
                done[_pair(post)] = _flag(meter, post, problems_so_far[_pair(post)], retry)
            break

        meter.event("rewrites_requested", retry=retry + 1, posts=[list(_pair(post)) for post, _ in rejected])
        rewrites = [Rewrite(post, for_the_writer(verdict)) for post, verdict in rejected]
        try:
            rewritten = {_pair(p): p for p in await agents.revise(key_moments, rewrites)}
        except AgentFailed as err:
            for post, _ in rejected:
                why = [*problems_so_far[_pair(post)], f"the Copywriter could not rewrite it: {err}"]
                done[_pair(post)] = _flag(meter, post, why, retry)
            break
        pending = []
        for post, _ in rejected:
            if _pair(post) in rewritten:
                pending.append(rewritten[_pair(post)])
            else:
                why = [*problems_so_far[_pair(post)], "the Copywriter returned no rewrite of it"]
                done[_pair(post)] = _flag(meter, post, why, retry)

    missing = drafted - set(done)
    if missing:  # can't happen: every post above ends passed or flagged
        raise RuntimeError(f"posts left neither passed nor flagged: {sorted(missing)}")
    return [done[pair] for pair in ALL_PAIRS if pair in done]


def _flag(meter: FilmMeter, post: SocialPost, problems: list[str], retries: int) -> CheckedPost:
    meter.event("post_flagged", platform=post.platform, language=post.language, retries=retries)
    return CheckedPost(post, "flagged", problems, retries)
