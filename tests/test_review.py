"""Tests for the retry loop between the Copywriter and the Checker: it fails closed, and no post skips the Checker."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trailer_to_campaign.agents.checker import Problem, Verdict
from trailer_to_campaign.agents.copywriter import Rewrite
from trailer_to_campaign.agents.key_moments import KeyMoment
from trailer_to_campaign.agents.loop import AgentFailed
from trailer_to_campaign.agents.review import MAX_RETRIES, Agents, CheckedPost, write_and_check
from trailer_to_campaign.agents.social_posts import ALL_PAIRS, SocialPost
from trailer_to_campaign.budget import FilmBudgetReached, FilmMeter
from trailer_to_campaign.run_log import RunLog

pytestmark = pytest.mark.anyio

SPOILER_LINE = "carry on with the procedure"
SPOILER_ID = "ending-capture"
SPOILER_REASON = "Points at the ending, where the agent is captured and recruited."
MOMENTS: list[KeyMoment] = []  # the loop passes them through; the fake agents don't read them


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def post(platform: str, language: str, text: str = "") -> SocialPost:
    return SocialPost(platform, language, text or f"A {platform} post in {language}. Stream it now.", [])


def draft(trap: bool = True) -> list[SocialPost]:
    posts = [post(p, lang) for p, lang in ALL_PAIRS]
    if trap:
        posts[0] = post("facebook", "en", f'"Doctor, {SPOILER_LINE}." Stream it now.')
    return posts


class FakeAgents:
    """A Checker that rejects any post with the spoiler line, and a Copywriter scripted to fix it or not."""

    def __init__(self, fixes_after: int | None = 1, first: list[SocialPost] | None = None) -> None:
        self.fixes_after = fixes_after  # the rewrite that fixes the post (None: never)
        self.first = first if first is not None else draft()
        self.writes = 0
        self.checked: list[list[SocialPost]] = []
        self.revised: list[list[Rewrite]] = []
        self.check_fails_on: int | None = None
        self.revise_fails = False
        self.check_raises: BaseException | None = None

    def agents(self) -> Agents:
        return Agents(write=self.write, revise=self.revise, check=self.check)

    async def write(self, key_moments: list[KeyMoment]) -> list[SocialPost]:
        self.writes += 1
        return self.first

    async def check(self, posts: list[SocialPost]) -> list[Verdict]:
        self.checked.append(posts)
        if self.check_raises:
            raise self.check_raises
        if self.check_fails_on == len(self.checked):
            raise AgentFailed("the Checker's verdicts did not hold after 3 tries")
        return [
            Verdict(False, [Problem("spoiler", SPOILER_LINE, SPOILER_REASON, spoiler_id=SPOILER_ID)])
            if SPOILER_LINE in p.text
            else Verdict(True, [])
            for p in posts
        ]

    async def revise(self, key_moments: list[KeyMoment], rewrites: list[Rewrite]) -> list[SocialPost]:
        self.revised.append(rewrites)
        if self.revise_fails:
            raise AgentFailed("the Copywriter's Social Posts were not traced after 3 tries")
        fixed = self.fixes_after is not None and len(self.revised) >= self.fixes_after
        return [
            replace(r.post, text="A spy walks into a barbershop. Stream it now.") if fixed else r.post for r in rewrites
        ]


@pytest.fixture
def meter(tmp_path: Path) -> FilmMeter:
    return FilmMeter(Decimal("2.00"), RunLog.create(tmp_path / "runs", "sample"))


def events(meter: FilmMeter, kind: str) -> list[dict[str, Any]]:
    lines = [json.loads(line) for line in meter.run_log.path.read_text().splitlines()]
    return [line for line in lines if line["event"] == kind]


def by_pair(checked: list[CheckedPost]) -> dict[tuple[str, str], CheckedPost]:
    return {(c.post.platform, c.post.language): c for c in checked}


def assert_no_post_skipped_the_checker(fake: FakeAgents, checked: list[CheckedPost]) -> None:
    """Every passed post is a post the Checker saw, word for word, and passed."""
    seen = {p.text for batch in fake.checked for p in batch}
    for c in checked:
        if c.status == "passed":
            assert c.post.text in seen


async def test_a_clean_draft_passes_after_one_check(meter: FilmMeter) -> None:
    fake = FakeAgents(first=draft(trap=False))
    checked = await write_and_check(MOMENTS, fake.agents(), meter)
    assert [(c.post.platform, c.post.language) for c in checked] == list(ALL_PAIRS)
    assert all(c.status == "passed" and c.retries == 0 and c.problems == [] for c in checked)
    assert len(fake.checked) == 1 and fake.revised == []


async def test_a_spoiler_is_caught_rewritten_and_passed_on_the_first_retry(meter: FilmMeter) -> None:
    fake = FakeAgents(fixes_after=1)
    checked = await write_and_check(MOMENTS, fake.agents(), meter)

    trap = by_pair(checked)[("facebook", "en")]
    assert trap.status == "passed" and trap.retries == 1 and SPOILER_LINE not in trap.post.text
    assert all(c.status == "passed" and c.retries == 0 for c in checked if c is not trap)
    [[rewrite]] = fake.revised  # only the rejected post goes back
    assert (rewrite.post.platform, rewrite.post.language) == ("facebook", "en")
    assert [len(batch) for batch in fake.checked] == [6, 1]  # the rewrite is checked again; passed posts are frozen
    assert_no_post_skipped_the_checker(fake, checked)


async def test_the_copywriter_hears_the_offending_words_but_never_the_spoiler_list(meter: FilmMeter) -> None:
    fake = FakeAgents(fixes_after=1)
    await write_and_check(MOMENTS, fake.agents(), meter)
    [[rewrite]] = fake.revised
    told = " ".join(rewrite.reasons)
    assert SPOILER_LINE in told  # words already in the writer's own post
    assert SPOILER_ID not in told and "captured" not in told and SPOILER_REASON not in told


async def test_a_post_still_rejected_after_two_retries_is_flagged_with_its_problem(meter: FilmMeter) -> None:
    fake = FakeAgents(fixes_after=None)
    checked = await write_and_check(MOMENTS, fake.agents(), meter)

    trap = by_pair(checked)[("facebook", "en")]
    assert trap.status == "flagged" and trap.retries == MAX_RETRIES == 2
    assert any(SPOILER_ID in p and SPOILER_REASON in p for p in trap.problems)  # the marketer sees why
    assert len(fake.checked) == 3 and len(fake.revised) == 2
    [flagged] = events(meter, "post_flagged")
    assert (flagged["platform"], flagged["language"], flagged["retries"]) == ("facebook", "en", 2)
    assert_no_post_skipped_the_checker(fake, checked)


async def test_a_checker_that_fails_flags_every_post_it_was_reviewing(meter: FilmMeter) -> None:
    fake = FakeAgents()
    fake.check_fails_on = 1
    checked = await write_and_check(MOMENTS, fake.agents(), meter)
    assert len(checked) == 6 and all(c.status == "flagged" for c in checked)
    assert all("Checker could not review" in c.problems[-1] for c in checked)


async def test_a_checker_that_fails_on_a_retry_keeps_the_passed_posts_and_flags_the_rest(meter: FilmMeter) -> None:
    fake = FakeAgents(fixes_after=1)
    fake.check_fails_on = 2
    checked = await write_and_check(MOMENTS, fake.agents(), meter)
    statuses = {pair: c.status for pair, c in by_pair(checked).items()}
    assert statuses.pop(("facebook", "en")) == "flagged"
    assert set(statuses.values()) == {"passed"}
    # The flagged post is the rewrite, which nobody reviewed: say so, and mark the old reasons as the old version's.
    rewrite = by_pair(checked)[("facebook", "en")]
    assert SPOILER_LINE not in rewrite.post.text
    assert "never reviewed" in rewrite.problems[0]
    assert all("version before" in p for p in rewrite.problems[1:])


async def test_a_copywriter_that_cannot_rewrite_leaves_the_post_flagged(meter: FilmMeter) -> None:
    fake = FakeAgents()
    fake.revise_fails = True
    checked = await write_and_check(MOMENTS, fake.agents(), meter)
    trap = by_pair(checked)[("facebook", "en")]
    assert trap.status == "flagged" and SPOILER_LINE in trap.post.text
    assert any("Copywriter could not rewrite" in p for p in trap.problems)
    assert any(SPOILER_ID in p for p in trap.problems)


async def test_a_first_draft_given_in_is_still_checked(meter: FilmMeter) -> None:
    fake = FakeAgents(fixes_after=1)
    planted = draft()
    checked = await write_and_check(MOMENTS, fake.agents(), meter, first_draft=planted)
    assert fake.writes == 0
    assert fake.checked[0] == planted
    assert_no_post_skipped_the_checker(fake, checked)


async def test_the_film_budget_stops_the_loop(meter: FilmMeter) -> None:
    fake = FakeAgents()
    fake.check_raises = FilmBudgetReached(Decimal("1.9"), Decimal("2"), Decimal("0.3"))
    with pytest.raises(FilmBudgetReached):
        await write_and_check(MOMENTS, fake.agents(), meter)


async def test_rewrites_are_logged(meter: FilmMeter) -> None:
    await write_and_check(MOMENTS, FakeAgents(fixes_after=1).agents(), meter)
    [requested] = events(meter, "rewrites_requested")
    assert requested["posts"] == [["facebook", "en"]] and requested["retry"] == 1


# From /code-review: an empty retry, a malformed planted draft, and the Film Budget mid-loop.


async def test_no_rewrites_back_means_no_empty_check(meter: FilmMeter) -> None:
    fake = FakeAgents()

    async def nothing(key_moments: list[KeyMoment], rewrites: list[Rewrite]) -> list[SocialPost]:
        return []

    agents = Agents(write=fake.write, revise=nothing, check=fake.check)
    checked = await write_and_check(MOMENTS, agents, meter)
    assert len(fake.checked) == 1  # never asked to judge zero posts
    assert any("no rewrite" in p for p in by_pair(checked)[("facebook", "en")].problems)


@pytest.mark.parametrize(
    "planted",
    [
        [*draft(), post("facebook", "en")],  # a pair twice: one would silently vanish
        [*draft()[:5], post("myspace", "en")],  # a pair no platform has
    ],
)
async def test_a_planted_draft_must_have_each_post_once(meter: FilmMeter, planted: list[SocialPost]) -> None:
    with pytest.raises(ValueError, match="first_draft"):
        await write_and_check(MOMENTS, FakeAgents().agents(), meter, first_draft=planted)


async def test_the_film_budget_mid_loop_keeps_what_the_checker_passed(meter: FilmMeter) -> None:
    from trailer_to_campaign.agents.review import FilmBudgetReachedMidReview

    fake = FakeAgents(fixes_after=1)
    real_check = fake.check

    async def budget_on_retry(posts: list[SocialPost]) -> list[Verdict]:
        if fake.checked:  # the first check went through; the re-check can't be paid for
            fake.checked.append(posts)
            raise FilmBudgetReached(Decimal("1.9"), Decimal("2"), Decimal("0.3"))
        return await real_check(posts)

    agents = Agents(write=fake.write, revise=fake.revise, check=budget_on_retry)
    with pytest.raises(FilmBudgetReachedMidReview) as caught:
        await write_and_check(MOMENTS, agents, meter)
    statuses = {pair: c.status for pair, c in by_pair(caught.value.checked).items()}
    assert statuses.pop(("facebook", "en")) == "flagged"
    assert set(statuses.values()) == {"passed"}  # paid for, and passed by the Checker: kept
    assert "Film Budget" in by_pair(caught.value.checked)[("facebook", "en")].problems[0]
