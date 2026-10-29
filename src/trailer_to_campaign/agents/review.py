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
from trailer_to_campaign.budget import FilmBudgetReached, FilmMeter
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


class FilmBudgetReachedMidReview(FilmBudgetReached):
    """The Film Budget ran out during the loop. The posts the Checker passed were paid for and are kept;
    every other post is flagged, saying it was not reviewed."""

    def __init__(self, reached: FilmBudgetReached, checked: list[CheckedPost]) -> None:
        super().__init__(reached.spent, reached.limit, reached.next_call)
        self.checked = checked


async def write_and_check(
    key_moments: list[KeyMoment], agents: Agents, meter: FilmMeter, first_draft: list[SocialPost] | None = None
) -> list[CheckedPost]:
    """The checked posts, in platform and language order.

    first_draft replaces the Copywriter's first draft (the Eval Set plants Traps this
    way); it must already be traced, and it is checked like any other draft.
    """
    if first_draft is not None:
        pairs = [_pair(p) for p in first_draft]
        if len(set(pairs)) != len(pairs) or not set(pairs) <= set(ALL_PAIRS):
            raise ValueError(f"first_draft must have each (platform, language) post at most once, got {pairs}")
    review = _Review(meter)
    review.offer(first_draft if first_draft is not None else await agents.write(key_moments))
    try:
        await review.run(key_moments, agents)
    except FilmBudgetReached as err:
        review.flag_the_rest("the Film Budget was reached")
        raise FilmBudgetReachedMidReview(err, review.checked()) from err
    return review.checked()


class _Review:
    """Each post's latest version, whether the Checker reviewed that version, and how it ended."""

    def __init__(self, meter: FilmMeter) -> None:
        self.meter = meter
        self.latest: dict[Pair, SocialPost] = {}
        self.reviewed: dict[Pair, bool] = {}  # was the latest version the one the Checker judged?
        self.problems: dict[Pair, list[str]] = {}  # the Checker's reasons, for the last version it rejected
        self.retries: dict[Pair, int] = {}
        self.done: dict[Pair, CheckedPost] = {}

    def offer(self, posts: list[SocialPost]) -> None:
        for post in posts:
            pair = _pair(post)
            self.latest[pair], self.reviewed[pair] = post, False
            self.retries[pair] = self.retries.get(pair, -1) + 1

    def pending(self) -> list[SocialPost]:
        return [post for pair, post in self.latest.items() if pair not in self.done and not self.reviewed[pair]]

    async def run(self, key_moments: list[KeyMoment], agents: Agents) -> None:
        for retry in range(MAX_RETRIES + 1):
            pending = self.pending()
            if not pending:
                return
            try:
                verdicts = await agents.check(pending)
            except AgentFailed as err:
                self.flag_the_rest(f"the Checker could not review it: {err}")
                return

            rejected = []
            for post, verdict in zip(pending, verdicts, strict=True):
                pair = _pair(post)
                self.reviewed[pair] = True
                if verdict.passed:
                    self.done[pair] = CheckedPost(post, "passed", [], self.retries[pair])
                else:
                    self.problems[pair] = for_the_marketer(verdict)
                    rejected.append((post, verdict))
            if not rejected:
                return
            if retry == MAX_RETRIES:
                for post, _ in rejected:
                    self._flag(_pair(post), self.problems[_pair(post)])
                return

            self.meter.event("rewrites_requested", retry=retry + 1, posts=[list(_pair(p)) for p, _ in rejected])
            rewrites = [Rewrite(post, for_the_writer(verdict)) for post, verdict in rejected]
            try:
                rewritten = {_pair(p): p for p in await agents.revise(key_moments, rewrites)}
            except AgentFailed as err:
                self.flag_the_rest(f"the Copywriter could not rewrite it: {err}")
                return
            for post, _ in rejected:
                if _pair(post) in rewritten:
                    self.offer([rewritten[_pair(post)]])
                else:
                    self._flag(_pair(post), [*self.problems[_pair(post)], "the Copywriter returned no rewrite of it"])

    def flag_the_rest(self, why: str) -> None:
        """Flag every post not yet passed or flagged, saying plainly whether its text was ever reviewed."""
        for pair in self.latest:
            if pair in self.done:
                continue
            if self.reviewed[pair]:  # rejected, and stopped before it could be rewritten
                self._flag(pair, [*self.problems[pair], f"not rewritten: {why}"])
            elif pair in self.problems:  # a rewrite nobody reviewed; the reasons are the old version's
                before = [f"the version before this rewrite was rejected: {p}" for p in self.problems[pair]]
                self._flag(pair, [f"this rewrite was never reviewed: {why}", *before])
            else:
                self._flag(pair, [f"this post was never reviewed: {why}"])

    def _flag(self, pair: Pair, problems: list[str]) -> None:
        post = self.latest[pair]
        self.meter.event("post_flagged", platform=post.platform, language=post.language, retries=self.retries[pair])
        self.done[pair] = CheckedPost(post, "flagged", problems, self.retries[pair])

    def checked(self) -> list[CheckedPost]:
        missing = set(self.latest) - set(self.done)
        if missing:  # can't happen: every post ends passed or flagged
            raise RuntimeError(f"posts left neither passed nor flagged: {sorted(missing)}")
        return [self.done[pair] for pair in ALL_PAIRS if pair in self.done]
