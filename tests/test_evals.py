"""Tests for the Eval Set: the Answer Key, scoring the Checker on it, and one Eval Set Run per Film (ADR 0008)."""

from __future__ import annotations

import asyncio
import builtins
import io
import json
import signal
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import anyio
import pytest
from fake_claude import FakeClient, reply, use

from trailer_to_campaign.agents.checker import Problem, Verdict
from trailer_to_campaign.agents.key_moments import KeyMoment, Quote
from trailer_to_campaign.agents.loop import AgentFailed
from trailer_to_campaign.agents.review import CheckedPost
from trailer_to_campaign.agents.social_posts import SocialPost
from trailer_to_campaign.budget import FilmBudgetReached, FilmMeter
from trailer_to_campaign.config import Settings
from trailer_to_campaign.evals import (
    AnswerKeyError,
    EvalFilm,
    EvalStages,
    load_answer_key,
    prepare,
    real_stages,
    run_eval,
)
from trailer_to_campaign.pricing import Usage

MODEL = "claude-opus-5-5"


def film_toml(title: str) -> str:
    return f"""
[catalog]
title = "{title}"
synopsis = "A short film."
genre = ["animation"]
language = "en"
runtime_minutes = 5
trailer = "https://www.youtube.com/watch?v=abc123"

[[catalog.cast]]
character = "Ellie"
name = "Ana Real"

[[spoilers]]
id = "the-twist"
text = "The sprites were the heroes all along."

[[spoilers]]
id = "the-ending"
text = "Everyone survives but the scout leader."
"""


ANSWER_KEY = """
[[published_posts]]
platform = "instagram"
language = "en"
text = "Our new short is out now!"
url = "https://example.org/post/1"
date = 2021-10-29

[[planted_drafts]]
id = "leak-twist"
trap = "spoiler"
spoiler_id = "the-twist"
platform = "facebook"
language = "en"
text = "Turns out the sprites were the heroes all along. Stream it now."

[[planted_drafts]]
id = "wrong-actor"
trap = "wrong_cast"
wrong_name = "Bob Fake"
platform = "tiktok"
language = "en"
text = "Bob Fake is unforgettable as Ellie. Stream it now."

[[clean_drafts]]
id = "clean"
platform = "instagram"
language = "vi"
text = "Một chuyến cắm trại không như mong đợi. Xem ngay."

[[input_traps]]
spoiler_id = "the-ending"
"""


@pytest.fixture
def eval_dir(tmp_path: Path) -> Path:
    root = tmp_path / "evals"
    for slug in ("alpha", "beta"):
        (root / "films" / slug).mkdir(parents=True)
        (root / "films" / slug / "film.toml").write_text(film_toml(slug.title()), encoding="utf-8")
    (root / "expected").mkdir()
    for slug in ("alpha", "beta"):
        (root / "expected" / f"{slug}.toml").write_text(ANSWER_KEY, encoding="utf-8")
    (root / "eval_set.toml").write_text('films = ["alpha", "beta", "gamma"]\n', encoding="utf-8")
    return root


def spoiler(spoiler_id: str, text: str) -> Problem:
    return Problem("spoiler", text, "Gives it away.", spoiler_id=spoiler_id)


def brand(text: str) -> Problem:
    return Problem("brand", text, "Not supported.", rule="invented_detail")


PASS = Verdict(True, [])


def moment() -> KeyMoment:
    return KeyMoment(1.0, 3.0, "Ellie runs.", "tense", Quote("Run!", 1.0, 2.0), ["Ellie"], [1])


def spend(meter: FilmMeter, agent: str) -> None:
    with meter.call(MODEL, 5_000, 4_000, agent=agent) as call:
        call.record(Usage(input=5_000, output=1_000))


class FakeStages:
    """Each stage spends through the real meter; the verdicts and errors are scripted per test."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.checked_drafts: list[SocialPost] = []
        self.draft_verdicts: list[Verdict] | BaseException = [
            Verdict(False, [spoiler("the-twist", "the sprites were the heroes")]),
            PASS,  # the Checker has no cast to compare against (#33), so a wrong name gets through
            PASS,
        ]
        self.pipeline_verdicts: list[Verdict] = [Verdict(False, [spoiler("the-ending", "but the scout leader")])]
        self.watch_raises: BaseException | None = None
        self.write_raises: dict[str, BaseException] = {}  # by Film: one Film's failure, the others fine
        self.posts = [
            CheckedPost(SocialPost("facebook", "en", "Alpha. Stream it now.", []), "passed", [], 1),
        ]

    def stages(self, film: EvalFilm) -> EvalStages:
        async def write_and_check(moments: list[KeyMoment], meter: FilmMeter, seen: list[Verdict]) -> Any:
            if film.slug in self.write_raises:
                spend(meter, "copywriter")
                raise self.write_raises[film.slug]
            return await self.write_and_check(moments, meter, seen)

        return EvalStages(check=self.check, watch=self.watch, write_and_check=write_and_check)

    async def check(self, posts: list[SocialPost], meter: FilmMeter) -> list[Verdict]:
        self.order.append("check")
        self.checked_drafts = posts
        spend(meter, "checker")
        if isinstance(self.draft_verdicts, BaseException):
            raise self.draft_verdicts
        return self.draft_verdicts

    async def watch(self, meter: FilmMeter) -> list[KeyMoment]:
        self.order.append("watch")
        if self.watch_raises:
            raise self.watch_raises
        spend(meter, "watcher")
        return [moment()]

    async def write_and_check(self, moments: list[KeyMoment], meter: FilmMeter, seen: list[Verdict]) -> Any:
        self.order.append("write_and_check")
        spend(meter, "copywriter")
        seen.extend(self.pipeline_verdicts)
        return self.posts


def run(eval_dir: Path, tmp_path: Path, fake: FakeStages, budget: str = "2.00") -> tuple[int, str]:
    out = io.StringIO()
    code = run_eval(prepare(eval_dir), Decimal(budget), tmp_path / "runs", fake.stages, out=out)
    return code, out.getvalue()


def summary(tmp_path: Path) -> dict[str, Any]:
    [path] = (tmp_path / "runs" / "_eval").glob("*/summary.json")
    return json.loads(path.read_text())


def run_log(tmp_path: Path, film: str) -> list[dict[str, Any]]:
    [path] = (tmp_path / "runs" / film).glob("*/run.jsonl")
    return [json.loads(line) for line in path.read_text().splitlines()]


# The Answer Key


def test_the_answer_key_is_read_with_every_kind_of_entry(eval_dir: Path) -> None:
    key = load_answer_key(eval_dir, "alpha", {"the-twist", "the-ending"})
    assert [p.url for p in key.published_posts] == ["https://example.org/post/1"]
    assert key.published_posts[0].date == "2021-10-29"
    assert [(d.id, d.trap) for d in key.planted_drafts] == [("leak-twist", "spoiler"), ("wrong-actor", "wrong_cast")]
    assert key.planted_drafts[1].wrong_name == "Bob Fake"
    assert [d.id for d in key.clean_drafts] == ["clean"]
    assert key.input_traps == ["the-ending"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda t: t.replace('trap = "spoiler"', 'trap = "spoiler"\ncolour = "red"', 1), "unknown keys"),
        (lambda t: t.replace('spoiler_id = "the-twist"', 'spoiler_id = "not-listed"'), "not on alpha's Spoiler List"),
        (lambda t: t.replace('spoiler_id = "the-ending"', 'spoiler_id = "nope"'), "not on alpha's Spoiler List"),
        (lambda t: t.replace('wrong_name = "Bob Fake"\n', ""), "wrong_name"),
        (lambda t: t.replace('trap = "spoiler"', 'trap = "typo"'), "trap must be"),
        (lambda t: t.replace('platform = "tiktok"', 'platform = "myspace"'), "platform"),
        (lambda t: t.replace('id = "clean"', 'id = "leak-twist"'), "used twice"),
        (lambda t: t + "\n[[planted_drafts]]\n", "needs"),
        # a field from the other kind of Trap would be silently dropped, so it is refused
        (lambda t: t.replace('trap = "spoiler"', 'trap = "spoiler"\nwrong_name = "Ana Real"'), "no wrong_name"),
        (lambda t: t.replace('trap = "wrong_cast"', 'trap = "wrong_cast"\nspoiler_id = "the-twist"'), "no spoiler_id"),
        (lambda t: t + '\n[[input_traps]]\nspoiler_id = "the-ending"\n', "listed twice"),
        # the Checker judges a draft like a real post, so one over its platform's limit is a bad entry
        (lambda t: t.replace("Bob Fake is unforgettable", "Bob Fake " * 20 + "is unforgettable"), "150 characters"),
    ],
)
def test_a_bad_answer_key_is_refused(eval_dir: Path, change: Any, message: str) -> None:
    path = eval_dir / "expected" / "alpha.toml"
    path.write_text(change(ANSWER_KEY), encoding="utf-8")
    with pytest.raises(AnswerKeyError, match=message):
        load_answer_key(eval_dir, "alpha", {"the-twist", "the-ending"})


def test_a_missing_film_folder_is_skipped_but_a_present_film_needs_its_answer_key(eval_dir: Path) -> None:
    films = prepare(eval_dir)
    assert [(f.slug, f.key is not None) for f in films] == [("alpha", True), ("beta", True), ("gamma", False)]
    (eval_dir / "expected" / "beta.toml").unlink()
    with pytest.raises(AnswerKeyError, match="beta"):
        prepare(eval_dir)


@pytest.mark.parametrize("films", ['["alpha", 3]', '[["alpha"]]', '"alpha"', "[]"])
def test_a_bad_eval_set_list_is_refused(eval_dir: Path, films: str) -> None:
    (eval_dir / "eval_set.toml").write_text(f"films = {films}\n", encoding="utf-8")
    with pytest.raises(AnswerKeyError, match="slugs"):
        prepare(eval_dir)


def test_prepare_keeps_each_films_trailer_so_it_is_read_once(eval_dir: Path) -> None:
    assert [f.trailer for f in prepare(eval_dir)] == ["https://www.youtube.com/watch?v=abc123"] * 2 + [""]


def test_a_film_with_no_trailer_link_is_refused_before_any_run(eval_dir: Path) -> None:
    path = eval_dir / "films" / "alpha" / "film.toml"
    path.write_text(film_toml("Alpha").replace('trailer = "https://www.youtube.com/watch?v=abc123"\n', ""))
    with pytest.raises(AnswerKeyError, match="trailer"):
        prepare(eval_dir)


# Scoring one Eval Set Run


def test_the_drafts_go_to_the_checker_first_and_are_scored(eval_dir: Path, tmp_path: Path) -> None:
    fake = FakeStages()
    code, out = run(eval_dir, tmp_path, fake)

    assert code == 0
    assert fake.order[:3] == ["check", "watch", "write_and_check"]
    assert [(p.platform, p.language, p.claims) for p in fake.checked_drafts] == [
        ("facebook", "en", []),
        ("tiktok", "en", []),
        ("instagram", "vi", []),
    ]
    [alpha, beta, gamma] = summary(tmp_path)["films"]
    assert alpha["planted"] == {
        "caught": 1,
        "total": 2,
        "by_trap": {"spoiler": [1, 1], "wrong_cast": [0, 1]},
        "missed": ["wrong-actor"],
        "not_reviewed": 0,
    }
    assert alpha["false_alarms"] == {"count": 0, "total": 1, "ids": []}
    assert alpha["input_traps"] == {"caught": 1, "total": 1, "not_caught": []}
    assert gamma["stop"] == "skipped"
    assert "Planted Drafts: caught 2 of 4" in out
    assert "Input Traps: caught 2 of 2" in out
    assert "Clean Drafts: false alarms 0 of 2" in out
    assert "gamma" in out and "skipped" in out


def test_a_spoiler_draft_caught_under_another_spoiler_id_is_not_caught(eval_dir: Path, tmp_path: Path) -> None:
    fake = FakeStages()
    fake.draft_verdicts = [
        Verdict(False, [spoiler("the-ending", "the sprites were the heroes")]),
        Verdict(False, [brand("is unforgettable as Ellie")]),  # rejected, but not for the wrong name
        PASS,
    ]
    run(eval_dir, tmp_path, fake)
    assert summary(tmp_path)["films"][0]["planted"]["missed"] == ["leak-twist", "wrong-actor"]


def test_a_wrong_name_is_caught_when_the_checker_points_at_it(eval_dir: Path, tmp_path: Path) -> None:
    fake = FakeStages()
    fake.draft_verdicts = [PASS, Verdict(False, [brand("Bob Fake is unforgettable")]), PASS]
    run(eval_dir, tmp_path, fake)
    planted = summary(tmp_path)["films"][0]["planted"]
    assert planted["by_trap"] == {"spoiler": [0, 1], "wrong_cast": [1, 1]}


def test_a_wrong_name_rejected_for_another_reason_is_not_caught(eval_dir: Path, tmp_path: Path) -> None:
    """Rejecting the sentence for its tone, with the name inside the quoted words, doesn't question the name."""
    fake = FakeStages()
    tone = Problem("brand", "Bob Fake is unforgettable as Ellie", "Too much hype.", rule="tone")
    fake.draft_verdicts = [PASS, Verdict(False, [tone]), PASS]
    run(eval_dir, tmp_path, fake)
    assert summary(tmp_path)["films"][0]["planted"]["by_trap"]["wrong_cast"] == [0, 1]


def test_a_rejected_clean_draft_is_a_false_alarm(eval_dir: Path, tmp_path: Path) -> None:
    fake = FakeStages()
    fake.draft_verdicts = [PASS, PASS, Verdict(False, [brand("Xem ngay")])]
    _, out = run(eval_dir, tmp_path, fake)
    assert summary(tmp_path)["films"][0]["false_alarms"] == {"count": 1, "total": 1, "ids": ["clean"]}
    assert "Clean Drafts: false alarms 2 of 2" in out


def test_a_checker_that_fails_on_the_drafts_catches_nothing(eval_dir: Path, tmp_path: Path) -> None:
    fake = FakeStages()
    fake.draft_verdicts = AgentFailed("the Checker gave up")
    code, out = run(eval_dir, tmp_path, fake)
    alpha = summary(tmp_path)["films"][0]
    # Fail closed both ways: unreviewed Planted Drafts are not caught, and unreviewed Clean Drafts are not clean.
    assert alpha["planted"]["caught"] == 0 and alpha["planted"]["missed"] == ["leak-twist", "wrong-actor"]
    assert alpha["planted"]["not_reviewed"] == 2
    assert alpha["false_alarms"] == {"count": 0, "total": 0, "ids": []}
    assert any("the Checker gave up" in e for e in alpha["errors"])
    assert fake.order.count("watch") == 2  # the pipeline still runs: it scores the input Traps
    assert "Planted Drafts: caught 0 of 4 (4 not reviewed: the Checker failed on them)" in out
    assert "Clean Drafts: false alarms 0 of 0" in out
    assert code == 1  # a Checker that couldn't review is never a clean run


def test_an_input_trap_no_verdict_names_is_listed_for_a_person(eval_dir: Path, tmp_path: Path) -> None:
    fake = FakeStages()
    fake.pipeline_verdicts = [PASS]
    run(eval_dir, tmp_path, fake)
    assert summary(tmp_path)["films"][0]["input_traps"] == {"caught": 0, "total": 1, "not_caught": ["the-ending"]}
    [report] = (tmp_path / "runs" / "_eval").glob("*/report.md")
    text = report.read_text()
    assert "the-ending" in text and "Alpha. Stream it now." in text  # the posts a person must read


def test_the_film_budget_in_the_watcher_keeps_the_planted_draft_score(eval_dir: Path, tmp_path: Path) -> None:
    fake = FakeStages()
    fake.watch_raises = FilmBudgetReached(Decimal("1.99"), Decimal("2.00"), Decimal("0.50"))
    code, _ = run(eval_dir, tmp_path, fake)
    alpha = summary(tmp_path)["films"][0]
    assert alpha["stop"] == "film_budget_reached"
    assert alpha["planted"]["caught"] == 1
    assert alpha["input_traps"] == {"caught": 0, "total": 1, "not_caught": ["the-ending"]}
    assert run_log(tmp_path, "alpha")[-1]["stop_reason"] == "film_budget_reached"
    assert code == 3


def test_an_unexpected_error_ends_that_films_run_and_the_eval_goes_on(eval_dir: Path, tmp_path: Path) -> None:
    """A model call that times out (an API error, not AgentFailed) must not lose every other Film's score."""
    fake = FakeStages()
    fake.write_raises["alpha"] = TimeoutError("the Copywriter's model call timed out")
    code, out = run(eval_dir, tmp_path, fake)
    alpha, beta, _ = summary(tmp_path)["films"]
    assert alpha["stop"] == "error"
    assert alpha["errors"] == ["TimeoutError: the Copywriter's model call timed out"]
    assert alpha["planted"]["caught"] == 1  # scored before the failure, so kept
    assert Decimal(alpha["cost_usd"]) > 0
    assert run_log(tmp_path, "alpha")[-1]["stop_reason"] == "error"
    assert beta["stop"] == "completed"
    assert "Planted Drafts: caught 2 of 4" in out
    assert code == 1


def test_a_film_whose_stages_cant_be_built_ends_in_error_and_the_eval_goes_on(eval_dir: Path, tmp_path: Path) -> None:
    fake = FakeStages()

    def stages(film: EvalFilm) -> EvalStages:
        if film.slug == "alpha":
            raise OSError("films folder went away")
        return fake.stages(film)

    out = io.StringIO()
    code = run_eval(prepare(eval_dir), Decimal("2.00"), tmp_path / "runs", stages, out=out)
    alpha, beta, _ = summary(tmp_path)["films"]
    assert alpha["stop"] == "error" and alpha["errors"] == ["OSError: films folder went away"]
    assert alpha["planted"]["not_reviewed"] == 2
    assert beta["stop"] == "completed"
    assert "Interrupted" not in out.getvalue()
    assert code == 1


def test_ctrl_c_still_writes_the_report_for_the_films_already_run(eval_dir: Path, tmp_path: Path) -> None:
    fake = FakeStages()
    fake.write_raises["beta"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run(eval_dir, tmp_path, fake)
    data = summary(tmp_path)
    assert [(f["film"], f["stop"]) for f in data["films"]] == [("alpha", "completed"), ("gamma", "skipped")]
    assert data["interrupted"] == ["beta"]  # T19: Films run side by side, so it names every Film not finished
    assert run_log(tmp_path, "beta")[-1]["stop_reason"] == "error"


# T19: the Films run side by side; the report keeps the Eval Set's order.


class SideBySide(FakeStages):
    """Watches yield, and track how many Films are in the Watcher at once."""

    def __init__(
        self,
        delays: dict[str, float] | None = None,
        meet: int = 0,
        fails: dict[str, BaseException] | None = None,
    ) -> None:
        super().__init__()
        self.delays, self.meet, self.fails = delays or {}, meet, fails or {}
        self.inside = self.most_inside = 0
        self.arrived: Any = None

    def stages(self, film: EvalFilm) -> EvalStages:
        base = super().stages(film)

        async def watch(meter: FilmMeter) -> list[KeyMoment]:
            self.inside += 1
            self.most_inside = max(self.most_inside, self.inside)
            if self.meet:  # every Film must be here before any goes on: a one-at-a-time run never gets past it
                self.arrived = self.arrived or anyio.Event()
                if self.inside == self.meet:
                    self.arrived.set()
                with anyio.fail_after(2):
                    await self.arrived.wait()
            await anyio.sleep(self.delays.get(film.slug, 0.01))
            self.inside -= 1
            if film.slug in self.fails:
                raise self.fails[film.slug]
            return await self.watch(meter)

        return EvalStages(check=base.check, watch=watch, write_and_check=base.write_and_check)


class CtrlC(FakeStages):
    """One Film's Watcher spends, then a real SIGINT arrives while its next call is still in flight."""

    def __init__(self, film: str, after: float = 0.05) -> None:
        super().__init__()
        self.film, self.after = film, after

    def stages(self, film: EvalFilm) -> EvalStages:
        base = super().stages(film)
        if film.slug != self.film:
            return base

        async def watch(meter: FilmMeter) -> list[KeyMoment]:
            spend(meter, "watcher")
            await anyio.sleep(self.after)
            signal.raise_signal(signal.SIGINT)
            await anyio.sleep(30)  # only being cancelled ends this
            raise AssertionError("Ctrl-C did not cancel the Film in flight")

        return EvalStages(check=base.check, watch=watch, write_and_check=base.write_and_check)


def run_with(eval_dir: Path, tmp_path: Path, fake: FakeStages, parallel: int | None = None) -> tuple[int, str]:
    out = io.StringIO()
    code = run_eval(prepare(eval_dir), Decimal("2.00"), tmp_path / "runs", fake.stages, out=out, parallel=parallel)
    return code, out.getvalue()


def test_the_films_run_side_by_side(eval_dir: Path, tmp_path: Path) -> None:
    fake = SideBySide(meet=2)  # one at a time would time out at the barrier
    code, _ = run_with(eval_dir, tmp_path, fake)
    assert code == 0 and fake.most_inside == 2


def test_the_report_keeps_the_eval_set_order_when_films_finish_out_of_order(eval_dir: Path, tmp_path: Path) -> None:
    fake = SideBySide(delays={"alpha": 0.2, "beta": 0.01})
    _, out = run_with(eval_dir, tmp_path, fake)
    assert out.index(f"{'beta':20} completed") < out.index(f"{'alpha':20} completed")  # beta ended first
    assert [f["film"] for f in summary(tmp_path)["films"]] == ["alpha", "beta", "gamma"]
    [report] = (tmp_path / "runs" / "_eval").glob("*/report.md")
    text = report.read_text()
    assert text.index("alpha") < text.index("beta")


def test_parallel_1_runs_one_film_at_a_time(eval_dir: Path, tmp_path: Path) -> None:
    fake = SideBySide(delays={"alpha": 0.05, "beta": 0.05})
    code, _ = run_with(eval_dir, tmp_path, fake, parallel=1)
    assert code == 0 and fake.most_inside == 1


def test_ctrl_c_cancels_a_film_in_flight_and_reports_the_ones_that_finished(eval_dir: Path, tmp_path: Path) -> None:
    with pytest.raises(KeyboardInterrupt):
        run_with(eval_dir, tmp_path, CtrlC("beta"))  # alpha has no wait, so it is done by then

    data = summary(tmp_path)
    assert [(f["film"], f["stop"]) for f in data["films"]] == [("alpha", "completed"), ("gamma", "skipped")]
    assert data["interrupted"] == ["beta"]
    last = run_log(tmp_path, "beta")[-1]
    assert last["stop_reason"] == "error" and Decimal(last["total_usd"]) > 0  # its spend is kept
    # In parallel a Film in flight can hold most of the spend, so the report says what the totals leave out.
    assert Decimal(data["interrupted_spent_usd"]) == Decimal(last["total_usd"])
    [report] = (tmp_path / "runs" / "_eval").glob("*/report.md")
    assert "Interrupted during beta" in report.read_text()


def test_no_new_film_starts_after_ctrl_c(eval_dir: Path, tmp_path: Path) -> None:
    with pytest.raises(KeyboardInterrupt):
        run_with(eval_dir, tmp_path, CtrlC("alpha"), parallel=1)

    data = summary(tmp_path)
    assert data["interrupted"] == ["alpha"] and data["not_started"] == ["beta"]
    assert run_log(tmp_path, "alpha")[-1]["stop_reason"] == "error"
    assert not (tmp_path / "runs" / "beta").exists()  # it never began, so it spent nothing


def test_a_stray_cancellation_is_not_a_clean_run(eval_dir: Path, tmp_path: Path) -> None:
    fake = SideBySide(fails={"alpha": asyncio.CancelledError()})  # from a library, with no Ctrl-C
    code, out = run_with(eval_dir, tmp_path, fake)

    assert code != 0  # the exit code gates prompt PRs: a run that scored nothing must not read as clean
    data = summary(tmp_path)
    assert "alpha" in data["interrupted"] + data.get("not_started", [])
    assert "alpha" in out and "totals cover only" in out


def test_a_closed_pipe_on_a_films_line_costs_no_film(eval_dir: Path, tmp_path: Path) -> None:
    class ClosedForAlpha(io.StringIO):
        def write(self, text: str) -> int:
            if text.startswith("alpha") and "completed" in text:
                raise BrokenPipeError
            return super().write(text)

    fake = SideBySide(delays={"alpha": 0.01, "beta": 0.1})
    code = run_eval(prepare(eval_dir), Decimal("2.00"), tmp_path / "runs", fake.stages, out=ClosedForAlpha())

    data = summary(tmp_path)
    assert code == 0 and "interrupted" not in data
    assert [(f["film"], f["stop"]) for f in data["films"]][:2] == [("alpha", "completed"), ("beta", "completed")]


def test_running_no_films_at_once_is_refused(eval_dir: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="parallel"):
        run_with(eval_dir, tmp_path, FakeStages(), parallel=0)


@pytest.mark.parametrize(
    ("failure", "stop"),
    [
        (RuntimeError("the Watcher crashed"), "error"),
        (FilmBudgetReached(Decimal("1.99"), Decimal("2.00"), Decimal("0.50")), "film_budget_reached"),
    ],
)
def test_a_film_failing_in_flight_leaves_the_other_films_score_alone(
    eval_dir: Path, tmp_path: Path, failure: BaseException, stop: str
) -> None:
    fake = SideBySide(delays={"alpha": 0.01, "beta": 0.1}, meet=2, fails={"alpha": failure})
    code, _ = run_with(eval_dir, tmp_path, fake)

    alpha, beta, _gamma = summary(tmp_path)["films"]
    assert code != 0 and alpha["stop"] == stop
    assert beta["stop"] == "completed" and beta["errors"] == [] and beta["watcher"]["key_moments"] == 1
    assert beta["planted"] == alpha["planted"]  # alpha's Planted Drafts were scored before it failed
    assert fake.most_inside == 2  # beta was in flight when alpha failed


def test_an_eval_run_asks_for_and_records_no_approval(
    eval_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_person(*args: Any) -> str:
        raise AssertionError("an Eval Set Run must never wait for a person")

    monkeypatch.setattr(builtins, "input", no_person)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(sys.stdin, "readline", no_person)
    code, _ = run(eval_dir, tmp_path, FakeStages())
    assert code == 0
    for film in ("alpha", "beta"):
        events = [e["event"] for e in run_log(tmp_path, film)]
        assert events.count("eval_run") == 1
        assert "approval" not in events and "approval_rate" not in events
        assert events[-1] == "run_finished"


def test_the_report_and_summary_hold_costs_and_posts_side_by_side(eval_dir: Path, tmp_path: Path) -> None:
    run(eval_dir, tmp_path, FakeStages())
    data = summary(tmp_path)
    alpha = data["films"][0]
    assert Decimal(alpha["cost_usd"]) > 0
    assert Decimal(data["totals"]["cost_usd"]) == sum(Decimal(f["cost_usd"]) for f in data["films"][:2])
    assert data["totals"]["planted"] == {"caught": 2, "total": 4, "not_reviewed": 0}
    assert data["totals"]["false_alarms"] == {"count": 0, "total": 2}
    assert data["totals"]["input_traps"] == {"caught": 2, "total": 2}
    assert data["skipped"] == ["gamma"]
    [report] = (tmp_path / "runs" / "_eval").glob("*/report.md")
    text = report.read_text()
    assert "Our new short is out now!" in text and "https://example.org/post/1" in text  # published
    assert "Alpha. Stream it now." in text  # generated, beside it


# T11: the Watcher's own numbers, so v0 and the SDK Watcher can be compared Film by Film.


class RejectedOnce(FakeStages):
    async def watch(self, meter: FilmMeter) -> list[KeyMoment]:
        meter.event("tool_call", agent="watcher", tool="get_catalog", is_error=False)
        meter.event("key_moments_rejected", agent="watcher", attempt=1, problems=["frame 999 was not seen"])
        spend(meter, "watcher")
        return await super().watch(meter)


def test_the_summary_has_the_watchers_own_numbers(eval_dir: Path, tmp_path: Path) -> None:
    run(eval_dir, tmp_path, RejectedOnce())
    data = summary(tmp_path)
    watcher_calls = [e for e in run_log(tmp_path, "alpha") if e["event"] == "model_call" and e["agent"] == "watcher"]
    assert data["films"][0]["watcher"] == {
        "cost_usd": str(sum(Decimal(e["cost_usd"]) for e in watcher_calls)),
        "model_calls": 2,
        "tool_calls": 1,
        "grounding_rejections": 1,
        "key_moments": 1,
    }
    totals = data["totals"]["watcher"]
    assert (totals["grounding_rejections"], totals["key_moments"], totals["model_calls"]) == (2, 2, 4)
    assert Decimal(totals["cost_usd"]) == 2 * Decimal(data["films"][0]["watcher"]["cost_usd"])


def test_a_half_written_run_log_line_never_loses_the_report(tmp_path: Path) -> None:
    from trailer_to_campaign.evals import FilmResult, _watcher_numbers

    log = tmp_path / "run.jsonl"
    whole = '{"event": "model_call", "agent": "watcher", "cost_usd": "0.01"}'
    log.write_text(whole + '\n{"event": "model_ca', encoding="utf-8")  # Ctrl-C mid-write
    numbers = _watcher_numbers(FilmResult("alpha", None, run_log=log))
    assert (numbers["model_calls"], numbers["cost_usd"]) == (1, "0.01")


def test_the_report_names_the_watcher_that_ran(eval_dir: Path, tmp_path: Path) -> None:
    out = io.StringIO()
    run_eval(prepare(eval_dir), Decimal("2"), tmp_path / "runs", FakeStages().stages, out=out, watcher="sdk")
    assert summary(tmp_path)["watcher"] == "sdk"
    assert "Watcher: sdk" in out.getvalue()
    [report] = (tmp_path / "runs" / "_eval").glob("*/report.md")
    assert "Watcher: sdk" in report.read_text()


# The real stages


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_the_real_checker_stage_reads_the_eval_sets_own_spoiler_list(eval_dir: Path, tmp_path: Path) -> None:
    from trailer_to_campaign.run_log import RunLog

    steps = [
        lambda params: reply(use("get_spoiler_list", film="alpha")),
        lambda params: reply(
            use(
                "submit_verdicts",
                verdicts=[
                    {
                        "post": 1,
                        "verdict": "reject",
                        "problems": [
                            {
                                "kind": "spoiler",
                                "spoiler_id": "the-twist",
                                "rule": None,
                                "offending_text": "heroes all along",
                                "reason": "The twist.",
                            }
                        ],
                    }
                ],
            )
        ),
    ]
    client = FakeClient(steps)
    settings = Settings(films_dir=eval_dir / "films")
    stages = real_stages("alpha", "https://www.youtube.com/watch?v=abc123", settings, lambda: client)
    meter = FilmMeter(Decimal("2.00"), RunLog.create(tmp_path / "runs", "alpha"))
    post = SocialPost("facebook", "en", "The sprites were the heroes all along.", [])
    [verdict] = await stages.check([post], meter)
    assert verdict.problems[0].spoiler_id == "the-twist"
