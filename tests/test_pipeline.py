"""Tests for one Run: Watcher -> Approval -> Copywriter + Checker -> Approval, under one Film Budget and one Run Log."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trailer_to_campaign.agents.checker import Verdict
from trailer_to_campaign.agents.key_moments import KeyMoment, Quote
from trailer_to_campaign.agents.loop import AgentFailed
from trailer_to_campaign.agents.review import CheckedPost, FilmBudgetReachedMidReview
from trailer_to_campaign.agents.social_posts import ALL_PAIRS, SocialPost
from trailer_to_campaign.approval import ApprovalAborted, KeyMomentDecision, PostDecision, Recheck
from trailer_to_campaign.budget import FilmBudgetReached, FilmMeter
from trailer_to_campaign.pipeline import Stages, run_film
from trailer_to_campaign.pricing import Usage

MODEL = "claude-opus-5-5"


def spend(meter: FilmMeter, agent: str) -> None:
    with meter.call(MODEL, 5_000, 4_000, agent=agent) as call:
        call.record(Usage(input=5_000, output=1_000))


def moment(i: int) -> KeyMoment:
    return KeyMoment(
        10.0 * i, 10.0 * i + 5, f"Moment {i}.", "tense", Quote(f"Line {i}.", 10.0 * i, 10.0 * i + 1), [], [i]
    )


class FakeStages:
    """Each stage spends through the real meter and logs the retry events the real agents log."""

    def __init__(self) -> None:
        self.wrote_from: list[list[KeyMoment]] = []
        self.rechecked: list[SocialPost] = []
        self.watch_raises: BaseException | None = None
        self.write_raises: BaseException | None = None
        self.recheck_raises: BaseException | None = None

    def stages(self) -> Stages:
        return Stages(watch=self.watch, write_and_check=self.write_and_check, recheck=self.recheck)

    async def watch(self, meter: FilmMeter) -> list[KeyMoment]:
        spend(meter, "watcher")
        if self.watch_raises:
            raise self.watch_raises
        meter.event("key_moments_rejected", agent="watcher", attempt=1, problems=["frame 36 is outside"])
        spend(meter, "watcher")
        return [moment(i) for i in range(1, 6)]

    async def write_and_check(self, key_moments: list[KeyMoment], meter: FilmMeter) -> list[CheckedPost]:
        self.wrote_from.append(key_moments)
        spend(meter, "copywriter")
        spend(meter, "checker")
        if self.write_raises:
            raise self.write_raises
        meter.event("rewrites_requested", retry=1, posts=[["instagram", "en"]])
        spend(meter, "copywriter")
        spend(meter, "checker")
        return [
            CheckedPost(SocialPost(p, lang, f"{p} {lang} post. Stream it now.", []), "passed", [], 0)
            for p, lang in ALL_PAIRS
        ]

    async def recheck(self, post: SocialPost, key_moments: list[KeyMoment], meter: FilmMeter) -> Recheck:
        self.rechecked.append(post)
        if self.recheck_raises:
            raise self.recheck_raises
        spend(meter, "checker")
        return Recheck([], Verdict(True, []))


class ScriptedApprover:
    """A marketer's decisions, given in order; the test fails if the pipeline asks for more or fewer."""

    def __init__(self, moments: list[KeyMomentDecision], posts: list[tuple[PostDecision, str | None]]) -> None:
        self.moments, self.posts = list(moments), list(posts)
        self.asked: list[str] = []

    def key_moment(self, i: int, n: int, moment: KeyMoment) -> KeyMomentDecision:
        self.asked.append(f"km {i}/{n}")
        return self.moments.pop(0)

    def social_post(self, i: int, n: int, checked: CheckedPost, recheck: Any) -> PostDecision:
        self.asked.append(f"post {i}/{n}")
        decision, edit = self.posts.pop(0)
        if edit is not None:
            self.last_recheck = recheck(edit)
        return decision


APPROVE_KM = KeyMomentDecision("approve")
APPROVE_POST = (PostDecision("approve"), None)


def marketer() -> ScriptedApprover:
    """Approves most, edits one of each (Light), rejects one of each."""
    return ScriptedApprover(
        [
            APPROVE_KM,
            KeyMomentDecision("edit", what_happens="Moment 2, as the marketer says it.", mood="sly", light=True),
            KeyMomentDecision("reject"),
            APPROVE_KM,
            APPROVE_KM,
        ],
        [
            (PostDecision("edit", text="A better post. Stream it now.", light=True), "A better post. Stream it now."),
            (PostDecision("reject"), None),
            APPROVE_POST,
            APPROVE_POST,
            APPROVE_POST,
            (
                PostDecision("edit", text="Rewritten hook. Stream it now.", light=False),
                "Rewritten hook. Stream it now.",
            ),
        ],
    )


def run(tmp_path: Path, stages: FakeStages, approver: Any, budget: str = "2.00") -> tuple[int, list[dict], Path]:
    out = io.StringIO()
    code = run_film("sample", Decimal(budget), tmp_path / "runs", stages.stages(), approver, out=out)
    [log] = (tmp_path / "runs").glob("sample/*/run.jsonl")
    run.out = out.getvalue()  # type: ignore[attr-defined]
    return code, [json.loads(line) for line in log.read_text().splitlines()], log.parent


def events(lines: list[dict], kind: str) -> list[dict]:
    return [line for line in lines if line["event"] == kind]


def test_one_run_shows_spend_retries_and_every_approval(tmp_path: Path) -> None:
    stages, approver = FakeStages(), marketer()
    code, lines, run_dir = run(tmp_path, stages, approver)

    assert code == 0
    finished = lines[-1]
    assert finished["event"] == "run_finished" and finished["stop_reason"] == "completed"
    calls = events(lines, "model_call")
    assert {c["agent"] for c in calls} == {"watcher", "copywriter", "checker"}  # spend, all in one Run Log
    assert Decimal(finished["total_usd"]) == sum(Decimal(c["cost_usd"]) for c in calls) <= Decimal("2.00")
    assert events(lines, "key_moments_rejected") and events(lines, "rewrites_requested")  # retries

    approvals = events(lines, "approval")
    assert [a["stage"] for a in approvals] == ["key_moments"] * 5 + ["social_posts"] * 6
    assert [a["decision"] for a in approvals] == ["approve", "edit", "reject", "approve", "approve"] + [
        "edit", "reject", "approve", "approve", "approve", "edit"
    ]  # fmt: skip
    assert approvals[1]["light"] is True and approvals[1]["what_happens"] == "Moment 2, as the marketer says it."
    assert approvals[5]["text"] == "A better post. Stream it now." and approvals[5]["platform"] == "facebook"
    assert approvals[10]["light"] is False
    assert len(approver.asked) == 11  # once per draft: nothing is approved without the marketer

    [rate] = events(lines, "approval_rate")
    assert rate["key_moments"] == "4/5" and rate["social_posts"] == "4/6" and rate["overall"] == "8/11"
    campaign = json.loads((run_dir / "campaign.json").read_text())
    assert (
        len(campaign["key_moments"]) == 5
        and campaign["social_posts"][0]["final_text"] == "A better post. Stream it now."
    )
    assert campaign["social_posts"][1]["final_text"] is None  # rejected
    assert (run_dir / "key_moments.json").is_file() and (run_dir / "social_posts.json").is_file()
    assert "Approval Rate" in run.out  # type: ignore[attr-defined]


def test_only_approved_moments_reach_the_copywriter_with_the_marketers_edits(tmp_path: Path) -> None:
    stages = FakeStages()
    run(tmp_path, stages, marketer())
    [given] = stages.wrote_from
    assert [m.start for m in given] == [10.0, 20.0, 40.0, 50.0]  # moment 3 was rejected
    assert given[1] == replace(moment(2), what_happens="Moment 2, as the marketer says it.", mood="sly")


def test_an_edited_post_is_rechecked_before_the_marketer_decides(tmp_path: Path) -> None:
    stages, approver = FakeStages(), marketer()
    run(tmp_path, stages, approver)
    assert [p.text for p in stages.rechecked] == ["A better post. Stream it now.", "Rewritten hook. Stream it now."]
    assert stages.rechecked[0].platform == "facebook" and stages.rechecked[0].language == "en"


def test_every_moment_rejected_ends_the_run_with_no_posts_and_no_writing_spend(tmp_path: Path) -> None:
    stages = FakeStages()
    approver = ScriptedApprover([KeyMomentDecision("reject")] * 5, [])
    code, lines, run_dir = run(tmp_path, stages, approver)
    assert code == 0 and lines[-1]["stop_reason"] == "completed"
    assert stages.wrote_from == []
    assert {c["agent"] for c in events(lines, "model_call")} == {"watcher"}
    assert json.loads((run_dir / "campaign.json").read_text())["social_posts"] == []


def test_the_film_budget_in_the_watcher_ends_the_run_before_any_approval(tmp_path: Path) -> None:
    stages, approver = FakeStages(), marketer()
    stages.watch_raises = FilmBudgetReached(Decimal("1.9"), Decimal("2"), Decimal("0.3"))
    code, lines, _ = run(tmp_path, stages, approver)
    assert code == 3 and lines[-1]["stop_reason"] == "film_budget_reached"
    assert approver.asked == []


def test_the_film_budget_mid_review_saves_what_passed_and_asks_nothing_more(tmp_path: Path) -> None:
    stages, approver = FakeStages(), marketer()
    passed = CheckedPost(SocialPost("facebook", "en", "Passed. Stream it now.", []), "passed", [], 0)
    stages.write_raises = FilmBudgetReachedMidReview(
        FilmBudgetReached(Decimal("1.9"), Decimal("2"), Decimal("0.3")), [passed]
    )
    code, lines, run_dir = run(tmp_path, stages, approver)
    assert code == 3 and lines[-1]["stop_reason"] == "film_budget_reached"
    assert json.loads((run_dir / "social_posts.json").read_text())[0]["post"]["text"] == "Passed. Stream it now."
    assert [a for a in approver.asked if a.startswith("post")] == []


def test_a_recheck_that_hits_the_film_budget_still_lets_the_marketer_decide(tmp_path: Path) -> None:
    stages, approver = FakeStages(), marketer()
    stages.recheck_raises = FilmBudgetReached(Decimal("1.9"), Decimal("2"), Decimal("0.3"))
    code, lines, _ = run(tmp_path, stages, approver)
    assert code == 0 and lines[-1]["stop_reason"] == "completed"
    assert "Film Budget" in (approver.last_recheck.note or "")
    assert len(events(lines, "approval")) == 11


def test_an_agent_that_gives_up_ends_the_run_in_an_error(tmp_path: Path) -> None:
    stages = FakeStages()
    stages.watch_raises = AgentFailed("the Watcher did not finish in 30 turns")
    code, lines, _ = run(tmp_path, stages, marketer())
    assert code == 1 and lines[-1]["stop_reason"] == "error"
    assert "30 turns" in run.out  # type: ignore[attr-defined]


@pytest.mark.parametrize("stop", [ApprovalAborted("the terminal closed"), KeyboardInterrupt()])
def test_a_marketer_who_leaves_ends_the_run_in_an_error_never_an_approval(tmp_path: Path, stop: BaseException) -> None:
    class Leaves(ScriptedApprover):
        def key_moment(self, i: int, n: int, moment: KeyMoment) -> KeyMomentDecision:
            if i == 2:
                raise stop
            return super().key_moment(i, n, moment)

    approver = Leaves([APPROVE_KM], [])
    if isinstance(stop, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            run_film("sample", Decimal("2"), tmp_path / "runs", FakeStages().stages(), approver, out=io.StringIO())
        [log] = (tmp_path / "runs").glob("sample/*/run.jsonl")
        lines = [json.loads(line) for line in log.read_text().splitlines()]
    else:
        code, lines, _ = run(tmp_path, FakeStages(), approver)
        assert code == 1
    assert lines[-1]["stop_reason"] == "error"
    assert len(events(lines, "approval")) == 1
