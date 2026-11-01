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
