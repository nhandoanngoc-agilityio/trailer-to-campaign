"""Tests for one Run: Watcher -> Approval -> Copywriter + Checker -> Approval, under one Film Budget and one Run Log."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trailer_to_campaign.agents.checker import Problem, Verdict
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
        self.rechecks: dict[str, Recheck] = {}  # by the text checked; anything else passes
        self.flagged: dict[tuple[str, str], list[str]] = {}  # posts the Checker still rejects after its retries

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
            CheckedPost(
                SocialPost(p, lang, f"{p} {lang} post. Stream it now.", []),
                "flagged" if (p, lang) in self.flagged else "passed",
                self.flagged.get((p, lang), []),
                0,
            )
            for p, lang in ALL_PAIRS
        ]

    async def recheck(self, post: SocialPost, key_moments: list[KeyMoment], meter: FilmMeter) -> Recheck:
        self.rechecked.append(post)
        if self.recheck_raises:
            raise self.recheck_raises
        spend(meter, "checker")
        return self.rechecks.get(post.text, Recheck([], Verdict(True, [])))


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


# The real recheck stage (tracing + the real Checker loop), with only the Claude API scripted.


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_the_real_recheck_traces_the_edit_and_asks_the_checker(tmp_path: Path) -> None:
    from fake_claude import FakeClient, reply, use

    from trailer_to_campaign.config import Settings
    from trailer_to_campaign.pipeline import real_stages
    from trailer_to_campaign.run_log import RunLog

    film = tmp_path / "films" / "sample"
    film.mkdir(parents=True)
    (film / "film.toml").write_text(
        '[catalog]\ntitle = "Sample Film"\nsynopsis = "A spy."\ngenre = ["comedy"]\nlanguage = "en"\n'
        'runtime_minutes = 4\n\n[[catalog.cast]]\ncharacter = "Agent 327"\n\n'
        '[[spoilers]]\nid = "ending"\ntext = "He is captured."\n',
        encoding="utf-8",
    )
    verdict = {"post": 1, "verdict": "pass", "problems": []}
    client = FakeClient(
        [
            lambda p: reply(use("get_spoiler_list", film="sample"), use("get_catalog", film="sample")),
            lambda p: reply(use("submit_verdicts", verdicts=[verdict])),
        ]
    )
    stages = real_stages("sample", "trailer.mp4", Settings(films_dir=tmp_path / "films"), lambda: client)
    edit = SocialPost("tiktok", "en", "A spy walks in. Stream it now.", [])  # no title, no claims: not traced
    meter = FilmMeter(Decimal("2"), RunLog.create(tmp_path / "runs", "sample"))

    result = await stages.recheck(edit, [moment(1)], meter)

    assert result.verdict == Verdict(True, [])
    assert any("Sample Film" in p for p in result.problems) and any("claim" in p for p in result.problems)
    task = client.messages.requests[0]["messages"][0]["content"]
    assert "A spy walks in." in task  # the Checker judged the marketer's edit


def test_each_stage_gets_a_client_of_its_own_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: one async client shared across stages failed in the second with 'Event loop is closed'."""
    import anyio
    from fake_claude import LoopBoundClient, reply, use

    from trailer_to_campaign.config import Settings
    from trailer_to_campaign.pipeline import real_stages
    from trailer_to_campaign.run_log import RunLog

    film = tmp_path / "films" / "sample"
    film.mkdir(parents=True)
    (film / "film.toml").write_text(
        '[catalog]\ntitle = "Sample Film"\nsynopsis = "A spy."\ngenre = ["comedy"]\nlanguage = "en"\n'
        'runtime_minutes = 4\n\n[[catalog.cast]]\ncharacter = "Agent 327"\n\n'
        '[[spoilers]]\nid = "ending"\ntext = "He is captured."\n',
        encoding="utf-8",
    )
    verdict = {"post": 1, "verdict": "pass", "problems": []}
    made: list[LoopBoundClient] = []

    def make_client() -> LoopBoundClient:
        made.append(
            LoopBoundClient(
                [
                    lambda p: reply(use("get_spoiler_list", film="sample"), use("get_catalog", film="sample")),
                    lambda p: reply(use("submit_verdicts", verdicts=[verdict])),
                ]
            )
        )
        return made[-1]

    from trailer_to_campaign import catalog

    reads: list[str] = []
    real_load = catalog.load_film
    monkeypatch.setattr(catalog, "load_film", lambda films_dir, f: reads.append(f) or real_load(films_dir, f))
    stages = real_stages("sample", "trailer.mp4", Settings(films_dir=tmp_path / "films"), make_client)
    meter = FilmMeter(Decimal("2"), RunLog.create(tmp_path / "runs", "sample"))
    edit = SocialPost("tiktok", "en", "A spy walks in. Stream it now.", [])
    for _ in range(2):  # two stages, two event loops, as in t2c run
        result = anyio.run(stages.recheck, edit, [moment(1)], meter)
        assert result.verdict == Verdict(True, [])
    assert len(made) == 2 and all(c.closed for c in made)
    assert reads == ["sample"]  # the catalog is read once per Run, not per re-check
    assert "aren't traced" in (result.note or "")  # a person's new facts can't be traced by code: say so


# T9: in Slack, the Run Log records which Marketer decided each draft.


def test_an_approval_records_who_decided_when_the_approver_knows(tmp_path: Path) -> None:
    approver = ScriptedApprover(
        [KeyMomentDecision("approve", by="U07A")] + [KeyMomentDecision("reject", by="U07B")] * 4,
        [(PostDecision("approve", by="U07A"), None)] * 6,
    )
    _, lines, _ = run(tmp_path, FakeStages(), approver)
    approvals = events(lines, "approval")
    assert [a["by"] for a in approvals] == ["U07A", "U07B", "U07B", "U07B", "U07B"] + ["U07A"] * 6


# T10: every edit's measured change is recorded beside its Light/heavy mark, as a sanity check.


def test_each_edit_records_its_measured_change_and_nothing_else_does(tmp_path: Path) -> None:
    _, lines, _ = run(tmp_path, FakeStages(), marketer())
    approvals = events(lines, "approval")
    edits = [a for a in approvals if a["decision"] == "edit"]
    assert [a["changed"] for a in edits] == pytest.approx([0.818, 0.333, 0.455], abs=0.01)
    assert all("changed" not in a for a in approvals if a["decision"] != "edit")


def test_the_approval_rate_comes_with_the_measured_change_and_light_edits_that_changed_a_lot(tmp_path: Path) -> None:
    _, lines, _ = run(tmp_path, FakeStages(), marketer())
    [rate] = events(lines, "approval_rate")
    assert rate["light_edit_change"]["count"] == 2
    assert rate["light_edit_change"]["mean"] == pytest.approx((0.818 + 0.333) / 2, abs=0.01)
    assert rate["heavy_edit_change"] == {"count": 1, "mean": pytest.approx(0.455, abs=0.01)}
    assert rate["light_edit_flag"] == 0.3  # the record names the threshold it used
    flagged = [(f["where"], f["changed"]) for f in rate["light_edits_over_flag"]]
    assert flagged == [
        ("key moment 2", pytest.approx(0.818, abs=0.01)),
        ("facebook en", pytest.approx(0.333, abs=0.01)),
    ]
    said = run.out  # type: ignore[attr-defined]
    assert "Measured change" in said and "key moment 2" in said


def test_a_run_with_no_edits_has_no_measured_change_to_report(tmp_path: Path) -> None:
    approver = ScriptedApprover([APPROVE_KM] * 5, [APPROVE_POST] * 6)
    _, lines, _ = run(tmp_path, FakeStages(), approver)
    [rate] = events(lines, "approval_rate")
    assert rate["light_edit_change"] == {"count": 0, "mean": None} and rate["light_edits_over_flag"] == []


def test_the_record_keeps_the_change_the_marketer_was_shown_and_who_wrote_the_edit(tmp_path: Path) -> None:
    shown = KeyMomentDecision("edit", what_happens="Moment 1, retold.", mood="tense", light=True, by="U07A")
    shown = replace(shown, edited_by="U07B", changed=0.42)  # what the approver measured and showed
    approver = ScriptedApprover([shown] + [APPROVE_KM] * 4, [APPROVE_POST] * 6)
    _, lines, _ = run(tmp_path, FakeStages(), approver)
    [edit] = [a for a in events(lines, "approval") if a["decision"] == "edit"]
    assert edit["changed"] == 0.42 and edit["edited_by"] == "U07B" and edit["by"] == "U07A"


def test_what_a_run_says_names_its_run_but_no_local_path(tmp_path: Path) -> None:
    """In Slack, everything the Run says is posted to the channel: no paths on the host machine."""
    run(tmp_path, FakeStages(), marketer())
    said = run.out  # type: ignore[attr-defined]
    assert str(tmp_path) not in said
    assert "runs/sample/" in said  # still enough for the team to find the Run Log


def test_a_terminal_approval_names_no_one(tmp_path: Path) -> None:
    _, lines, _ = run(tmp_path, FakeStages(), marketer())
    assert all("by" not in a for a in events(lines, "approval"))


# From /code-review: a re-check never ends the Run, and what was decided survives an early stop.


@pytest.mark.parametrize("failure", [OSError("the checker server did not start"), ValueError("film.toml changed")])
def test_any_recheck_failure_is_shown_and_never_ends_the_run(tmp_path: Path, failure: Exception) -> None:
    stages, approver = FakeStages(), marketer()
    stages.recheck_raises = failure
    code, lines, _ = run(tmp_path, stages, approver)
    assert code == 0 and lines[-1]["stop_reason"] == "completed"
    assert "not re-checked" in (approver.last_recheck.note or "")
    assert len(events(lines, "approval")) == 11


def test_an_early_stop_keeps_the_decided_key_moments_and_posts(tmp_path: Path) -> None:
    class LeavesAtPost3(ScriptedApprover):
        def social_post(self, i: int, n: int, checked: CheckedPost, recheck: Any) -> PostDecision:
            if i == 3:
                raise ApprovalAborted("the terminal closed")
            return super().social_post(i, n, checked, recheck)

    m = marketer()
    approver = LeavesAtPost3(m.moments, m.posts)
    code, lines, run_dir = run(tmp_path, FakeStages(), approver)
    assert code == 1 and lines[-1]["stop_reason"] == "error"
    campaign = json.loads((run_dir / "campaign.json").read_text())
    assert campaign["stop_reason"] == "error"
    assert campaign["key_moments"][1]["key_moment"]["what_happens"] == "Moment 2, as the marketer says it."
    assert [p["decision"] for p in campaign["social_posts"]] == ["edit", "reject"]


def test_the_film_budget_mid_review_keeps_the_key_moment_decisions(tmp_path: Path) -> None:
    stages = FakeStages()
    stages.write_raises = FilmBudgetReachedMidReview(
        FilmBudgetReached(Decimal("1.9"), Decimal("2"), Decimal("0.3")), []
    )
    _, _, run_dir = run(tmp_path, stages, marketer())
    campaign = json.loads((run_dir / "campaign.json").read_text())
    assert campaign["stop_reason"] == "film_budget_reached" and len(campaign["key_moments"]) == 5


# T11: the Watcher stage runs on the harness the settings name.


@pytest.mark.parametrize("harness", ["v0", "sdk"])
def test_the_watcher_stage_runs_the_harness_the_settings_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    import anyio

    from trailer_to_campaign.agents import watcher, watcher_sdk
    from trailer_to_campaign.config import Settings
    from trailer_to_campaign.pipeline import watch_stage
    from trailer_to_campaign.run_log import RunLog

    ran: list[tuple[str, Any]] = []

    async def v0(film: str, source: str, *, servers: Any, client: Any, meter: Any) -> list[KeyMoment]:
        ran.append(("v0", client.name))
        return [moment(1)]

    async def sdk(film: str, source: str, *, servers: Any, meter: Any, api_key: str) -> list[KeyMoment]:
        ran.append(("sdk", api_key))
        return [moment(1)]

    class Client:
        name = "client"

        async def close(self) -> None:
            pass

    monkeypatch.setattr(watcher, "watch_trailer", v0)
    monkeypatch.setattr(watcher_sdk, "watch_trailer_sdk", sdk)
    monkeypatch.setattr(watcher, "watcher_servers", lambda settings: ["footage", "catalog"])
    settings = Settings(watcher=harness, _api_key="sk-test")
    meter = FilmMeter(Decimal("2"), RunLog.create(tmp_path, "sample"))
    assert anyio.run(watch_stage("sample", "trailer.mp4", settings, Client), meter) == [moment(1)]
    assert ran == ([("v0", "client")] if harness == "v0" else [("sdk", "sk-test")])


# T17: a model call that never answers ends the Run in an error, with the Run Log finished and the spend kept.


def test_a_model_call_that_never_answers_ends_the_run_in_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fake_claude import HangingClient

    from trailer_to_campaign.agents import loop

    monkeypatch.setattr(loop, "MODEL_CALL_TIMEOUT_S", 0.05)

    class HungWatcher(FakeStages):
        async def watch(self, meter: FilmMeter) -> list[KeyMoment]:
            spend(meter, "watcher")  # a first call answers; the next one never does
            agent = loop.AgentLoop(HangingClient(), meter, "watcher", MODEL, "medium", "", [], 3, "Go on.")
            await agent.run([{"role": "user", "content": "Watch."}], call_tool=None, is_done=lambda: False)  # type: ignore[arg-type]
            return []

    approver = marketer()
    code, lines, _ = run(tmp_path, HungWatcher(), approver)
    assert code == 1 and lines[-1]["event"] == "run_finished" and lines[-1]["stop_reason"] == "error"
    answered, hung = events(lines, "model_call")
    assert hung["error"] == "TimeoutError" and hung["charged"] == "worst_case"
    assert Decimal(lines[-1]["total_usd"]) == Decimal(answered["cost_usd"]) + Decimal(hung["cost_usd"])
    assert "did not answer within" in run.out  # type: ignore[attr-defined]
    assert approver.asked == []  # nothing reaches Approval


# T22: a post approved over a flag says so, in the record and in the numbers.


EDITED = "An edited post. Stream it now."
REJECTED = Problem("brand", "edited", "A word is missing.", rule="other")
CHECKER_FLAGGED = Recheck([], Verdict(False, [REJECTED]))
TRACE_FLAGGED = Recheck(["quote 'x' is not a Key Moment's line"], Verdict(True, []))


class EditsAgain(ScriptedApprover):
    """Edits one post several times before deciding, as a Marketer can."""

    def __init__(self, edits: list[str], decision: PostDecision) -> None:
        super().__init__([APPROVE_KM] * 5, [APPROVE_POST] * 5)
        self.edits, self.decision = edits, decision

    def social_post(self, i: int, n: int, checked: CheckedPost, recheck: Any) -> PostDecision:
        if i > 1:
            return super().social_post(i, n, checked, recheck)
        for text in self.edits:
            recheck(text)
        return self.decision


def edit_of_first_post(stages: FakeStages, tmp_path: Path, edits: list[str], **decision: Any) -> tuple[Any, Any, Any]:
    made = PostDecision("edit", text=edits[-1], light=decision.get("light", True))
    _, lines, run_dir = run(tmp_path, stages, EditsAgain(edits, made))
    [approval] = [a for a in events(lines, "approval") if a.get("platform") == "facebook" and a["language"] == "en"]
    first = json.loads((run_dir / "campaign.json").read_text())["social_posts"][0]
    [rate] = events(lines, "approval_rate")
    return approval, first, rate


def test_an_edit_the_checker_passes_records_passed(tmp_path: Path) -> None:
    approval, first, rate = edit_of_first_post(FakeStages(), tmp_path, [EDITED])
    assert approval["edit_check"] == first["edit_check"] == "passed"
    assert approval["edit_problems"] == first["edit_problems"] == []
    assert rate["approved_over_a_flag"] == [] and rate["approved_not_rechecked"] == []


def test_an_edit_the_checker_rejects_and_a_marketer_approves_records_flagged(tmp_path: Path) -> None:
    stages = FakeStages()
    stages.rechecks[EDITED] = CHECKER_FLAGGED
    approval, first, rate = edit_of_first_post(stages, tmp_path, [EDITED])

    assert approval["edit_check"] == first["edit_check"] == "flagged"
    assert (
        any("A word is missing." in p for p in first["edit_problems"])
        and approval["edit_problems"] == first["edit_problems"]
    )
    assert first["status"] == "passed"  # the draft's status, before the edit: unchanged, and beside the new one
    assert first["final_text"] == EDITED
    assert rate["approved_over_a_flag"] == ["facebook en"]


def test_an_edit_with_only_a_tracing_problem_records_flagged(tmp_path: Path) -> None:
    stages = FakeStages()
    stages.rechecks[EDITED] = TRACE_FLAGGED
    _, first, rate = edit_of_first_post(stages, tmp_path, [EDITED])

    assert first["edit_check"] == "flagged" and first["edit_problems"] == TRACE_FLAGGED.problems
    assert rate["approved_over_a_flag"] == ["facebook en"]


@pytest.mark.parametrize(
    "failure",
    [
        FilmBudgetReached(Decimal("1.9"), Decimal("2"), Decimal("0.3")),
        AgentFailed("the Checker gave up"),
        ValueError("oops"),
    ],
)
def test_an_edit_whose_recheck_could_not_run_records_not_rechecked(tmp_path: Path, failure: Exception) -> None:
    stages = FakeStages()
    stages.recheck_raises = failure
    _, first, rate = edit_of_first_post(stages, tmp_path, [EDITED])

    assert first["edit_check"] == "not_rechecked" and first["edit_problems"]  # it says why
    assert rate["approved_not_rechecked"] == ["facebook en"] and rate["approved_over_a_flag"] == []
    assert "approved with no re-check: facebook en" in run.out  # type: ignore[attr-defined]


def test_an_edit_decided_with_no_recheck_of_its_text_records_not_rechecked(tmp_path: Path) -> None:
    decision = PostDecision("edit", text="Text nobody re-checked. Stream it now.", light=True)
    _, lines, run_dir = run(tmp_path, FakeStages(), EditsAgain([EDITED], decision))
    first = json.loads((run_dir / "campaign.json").read_text())["social_posts"][0]
    assert first["edit_check"] == "not_rechecked"
    assert events(lines, "approval_rate")[0]["approved_not_rechecked"] == ["facebook en"]


@pytest.mark.parametrize(
    ("flagged_text", "edits", "expected"),
    [
        ("First try. Stream it now.", ["First try. Stream it now.", "Second try. Stream it now."], "passed"),
        ("Second try. Stream it now.", ["First try. Stream it now.", "Second try. Stream it now."], "flagged"),
    ],
)
def test_only_the_recheck_of_the_final_text_counts(
    tmp_path: Path, flagged_text: str, edits: list[str], expected: str
) -> None:
    stages = FakeStages()
    stages.rechecks[flagged_text] = CHECKER_FLAGGED
    _, first, _ = edit_of_first_post(stages, tmp_path, edits)
    assert first["edit_check"] == expected


def test_a_post_with_no_edit_and_a_rejected_edit_keep_todays_record(tmp_path: Path) -> None:
    stages = FakeStages()
    stages.rechecks[EDITED] = CHECKER_FLAGGED
    _, lines, run_dir = run(tmp_path, stages, EditsAgain([EDITED], PostDecision("reject")))

    campaign = json.loads((run_dir / "campaign.json").read_text())["social_posts"]
    posts = [a for a in events(lines, "approval") if a["stage"] == "social_posts"]
    assert all("edit_check" not in p and "edit_problems" not in p for p in [*campaign, *posts])
    [rate] = events(lines, "approval_rate")
    assert rate["approved_over_a_flag"] == []  # rejected: it won't go out


def test_a_flagged_post_approved_unedited_counts_as_approved_over_a_flag(tmp_path: Path) -> None:
    stages = FakeStages()
    stages.flagged[("tiktok", "vi")] = ["spoiler the-end: gives it away"]
    approver = ScriptedApprover([APPROVE_KM] * 5, [APPROVE_POST] * 6)
    _, lines, run_dir = run(tmp_path, stages, approver)

    assert events(lines, "approval_rate")[0]["approved_over_a_flag"] == ["tiktok vi"]
    assert "edit_check" not in json.loads((run_dir / "campaign.json").read_text())["social_posts"][5]


def test_a_flagged_post_rejected_is_not_approved_over_a_flag(tmp_path: Path) -> None:
    stages = FakeStages()
    stages.flagged[("facebook", "en")] = ["spoiler the-end: gives it away"]
    approver = ScriptedApprover([APPROVE_KM] * 5, [(PostDecision("reject"), None), *[APPROVE_POST] * 5])
    _, lines, _ = run(tmp_path, stages, approver)
    assert events(lines, "approval_rate")[0]["approved_over_a_flag"] == []


def test_a_flagged_post_whose_edit_passes_is_not_approved_over_a_flag(tmp_path: Path) -> None:
    stages = FakeStages()
    stages.flagged[("facebook", "en")] = ["spoiler the-end: gives it away"]
    _, first, rate = edit_of_first_post(stages, tmp_path, [EDITED])
    assert first["edit_check"] == "passed" and rate["approved_over_a_flag"] == []


def test_a_heavy_edit_over_a_flag_counts_though_it_is_not_in_the_approval_rate(tmp_path: Path) -> None:
    stages = FakeStages()
    stages.rechecks[EDITED] = CHECKER_FLAGGED
    _, _, rate = edit_of_first_post(stages, tmp_path, [EDITED], light=False)
    assert rate["approved_over_a_flag"] == ["facebook en"]
    assert rate["social_posts"] == "5/6"  # a heavy edit was never in it


def test_approving_over_a_flag_does_not_change_the_approval_rate(tmp_path: Path) -> None:
    stages = FakeStages()
    stages.rechecks[EDITED] = CHECKER_FLAGGED
    _, _, flagged_rate = edit_of_first_post(stages, tmp_path / "flagged", [EDITED])
    _, _, passed_rate = edit_of_first_post(FakeStages(), tmp_path / "passed", [EDITED])
    assert flagged_rate["social_posts"] == passed_rate["social_posts"] == "6/6"


def test_the_run_says_what_was_approved_over_a_flag_by_name_and_not_why(tmp_path: Path) -> None:
    stages = FakeStages()
    stages.rechecks[EDITED] = CHECKER_FLAGGED
    edit_of_first_post(stages, tmp_path, [EDITED])
    said = run.out  # type: ignore[attr-defined]
    assert "over a flag: facebook en" in said
    assert "A word is missing." not in said  # in Slack, everything the Run says goes to the channel


def test_a_clean_run_says_nothing_about_flags(tmp_path: Path) -> None:
    edit_of_first_post(FakeStages(), tmp_path, [EDITED])
    assert "over a flag" not in run.out and "no re-check" not in run.out  # type: ignore[attr-defined]


def test_a_tracing_problem_stays_on_the_record_when_the_checker_could_not_run(tmp_path: Path) -> None:
    stages = FakeStages()
    stages.rechecks[EDITED] = Recheck(["quote 'x' is not a Key Moment's line"], None, note="not re-checked: it failed")
    _, first, rate = edit_of_first_post(stages, tmp_path, [EDITED])

    assert first["edit_check"] == "flagged"  # a known objection, not just a gap
    assert first["edit_problems"] == ["quote 'x' is not a Key Moment's line", "not re-checked: it failed"]
    assert rate["approved_over_a_flag"] == ["facebook en"] and rate["approved_not_rechecked"] == []


@pytest.mark.parametrize(
    "failure",
    [
        AgentFailed("the Checker gave up"),
        FilmBudgetReached(Decimal("1.9"), Decimal("2"), Decimal("0.3")),
        ValueError("oops"),
    ],
)
def test_the_real_recheck_keeps_what_tracing_found_when_the_checker_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    import anyio
    from fake_claude import FakeClient

    from trailer_to_campaign.agents import checker
    from trailer_to_campaign.config import Settings
    from trailer_to_campaign.pipeline import real_stages
    from trailer_to_campaign.run_log import RunLog

    film = tmp_path / "films" / "sample"
    film.mkdir(parents=True)
    (film / "film.toml").write_text(
        '[catalog]\ntitle = "Sample Film"\nsynopsis = "A spy."\ngenre = ["comedy"]\nlanguage = "en"\n'
        'runtime_minutes = 4\n\n[[catalog.cast]]\ncharacter = "Agent 327"\n\n'
        '[[spoilers]]\nid = "ending"\ntext = "He is captured."\n',
        encoding="utf-8",
    )

    async def fails(*args: Any, **kwargs: Any) -> Any:
        raise failure

    monkeypatch.setattr(checker, "check_posts", fails)
    stages = real_stages("sample", "trailer.mp4", Settings(films_dir=tmp_path / "films"), lambda: FakeClient([]))
    meter = FilmMeter(Decimal("2"), RunLog.create(tmp_path / "runs", "sample"))
    no_title = SocialPost("tiktok", "en", "A spy walks in. Stream it now.", [])
    result = anyio.run(stages.recheck, no_title, [moment(1)], meter)

    assert any("name the Film" in p for p in result.problems)  # tracing's finding survives
    assert result.verdict is None and "not re-checked" in (result.note or "")


def test_a_failed_save_never_prints_the_checkers_reasons_to_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """In Slack, what the Run says goes to the channel, and a reason can describe the spoiler (ADR 0007)."""
    real_write = Path.write_text

    def write_text(self: Path, *args: Any, **kwargs: Any) -> int:
        if self.name == "campaign.json":
            raise OSError("the disk is full")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text)
    stages = FakeStages()
    stages.rechecks[EDITED] = CHECKER_FLAGGED
    decision = PostDecision("edit", text=EDITED, light=True)
    code, _, _ = run(tmp_path, stages, EditsAgain([EDITED], decision))

    assert code == 0 and "A word is missing." not in run.out  # type: ignore[attr-defined]
    assert "A word is missing." in capsys.readouterr().err  # paid for and decided: still not lost


def test_an_edit_approved_over_a_flag_in_the_terminal_is_on_the_record(tmp_path: Path) -> None:
    from trailer_to_campaign.approval import TerminalApprover

    answers = iter(["a"] * 5 + ["e", EDITED, ".", "a", "l"] + ["a"] * 5)
    stages = FakeStages()
    stages.rechecks[EDITED] = CHECKER_FLAGGED
    approver = TerminalApprover(input=lambda prompt: next(answers), out=io.StringIO())
    _, lines, run_dir = run(tmp_path, stages, approver)

    first = json.loads((run_dir / "campaign.json").read_text())["social_posts"][0]
    assert first["edit_check"] == "flagged" and first["final_text"] == EDITED
    assert events(lines, "approval_rate")[0]["approved_over_a_flag"] == ["facebook en"]


def test_a_terminal_edit_with_spaces_around_it_still_finds_its_recheck(tmp_path: Path) -> None:
    """The record finds a re-check by its text, so an Approver that tidied one side would lose it."""
    from trailer_to_campaign.approval import TerminalApprover

    answers = iter(["a"] * 5 + ["e", f"  {EDITED}  ", ".", "a", "l"] + ["a"] * 5)
    approver = TerminalApprover(input=lambda prompt: next(answers), out=io.StringIO())
    _, _, run_dir = run(tmp_path, FakeStages(), approver)

    first = json.loads((run_dir / "campaign.json").read_text())["social_posts"][0]
    assert first["edit_check"] == "passed"
