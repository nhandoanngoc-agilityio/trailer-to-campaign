"""Tests for the side-by-side result (T12): Time Saved, Approval Rate and cost per Film, from a Film's own records."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trailer_to_campaign.result import FilmResult, film_result, write_result

FILM = "quiet-river"
FILM_TOML = """
[catalog]
title = "The Quiet River"
synopsis = "A ferryman's last crossing."
genre = ["drama"]
language = "vi"
runtime_minutes = 98

[[catalog.cast]]
character = "Ba"

[[spoilers]]
id = "ending"
text = "The ferryman drowns at the end."
"""
POST = "Ba rows into the dawn. Stream it now."
MOMENT = "Ba ties the boat to a dead tree."


@pytest.fixture
def films(tmp_path: Path) -> Path:
    film = tmp_path / "films" / FILM
    film.mkdir(parents=True)
    (film / "film.toml").write_text(FILM_TOML, encoding="utf-8")
    return tmp_path / "films"


def a_run(
    runs: Path,
    stamp: str,
    *,
    costs: list[str],
    end: str | None,
    minutes: float = 10,
    rate: dict[str, Any] | None = None,
    agent: str = "watcher",
    extra: list[dict[str, Any]] | None = None,
) -> Path:
    """One Run Log under runs/<film>/<stamp>/, as the pipeline writes it; end=None is a Run killed mid-way."""
    folder = runs / FILM / stamp
    folder.mkdir(parents=True)
    began = datetime.strptime(stamp[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    start = began.isoformat()
    lines: list[dict[str, Any]] = [{"event": "run_started", "at": start, "film": FILM}, *(extra or [])]
    lines += [{"event": "model_call", "at": start, "agent": agent, "cost_usd": c} for c in costs]
    if rate is not None:
        lines.append({"event": "approval_rate", "at": start, **rate})
    if end is not None:
        finish = (began + timedelta(minutes=minutes)).isoformat()
        total = str(sum((Decimal(c) for c in costs), Decimal(0)))
        lines.append({"event": "run_finished", "at": finish, "total_usd": total, "stop_reason": end})
    (folder / "run.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    (folder / "campaign.json").write_text(json.dumps({"social_posts": [{"text": POST}]}), encoding="utf-8")
    (folder / "key_moments.json").write_text(json.dumps([{"what_happens": MOMENT}]), encoding="utf-8")
    return folder


RATE = {
    "key_moments": "8/9",
    "social_posts": "5/6",
    "overall": "13/15",
    "light_edit_change": {"count": 2, "mean": 0.21},
    "heavy_edit_change": {"count": 1, "mean": 0.64},
    "light_edit_flag": 0.3,
    "light_edits_over_flag": [{"where": "tiktok vi", "changed": 0.41}],
    "approved_over_a_flag": ["tiktok vi"],
    "approved_not_rechecked": [],
}


def trial(runs: Path) -> None:
    """A failed Run, a Run killed mid-way, an early completed one, and the re-run whose Campaign was used."""
    a_run(runs, "20261102T010000Z", costs=["0.30"], end="error", minutes=5)
    a_run(runs, "20261102T020000Z", costs=["0.10", "0.05"], end=None)
    a_run(runs, "20261102T030000Z", costs=["0.40"], end="completed", minutes=20,
          rate={**RATE, "overall": "9/15", "key_moments": "5/9"})  # fmt: skip
    a_run(runs, "20261102T040000Z", costs=["0.45", "0.05"], end="completed", minutes=75, rate=RATE)


def test_cost_per_film_is_every_run_re_runs_and_failures_included(tmp_path: Path, films: Path) -> None:
    trial(tmp_path / "runs")
    result = film_result(FILM, tmp_path / "runs", films, old_minutes=240, new_minutes=60, old_way_source="stopwatch")
    assert result.cost == Decimal("1.35")  # 0.30 + 0.15 + 0.40 + 0.50
    assert [r.ended for r in result.runs] == ["error", "did not finish", "completed", "completed"]
    assert result.runs[1].cost == Decimal("0.15")  # no run_finished: its model calls, which were spent


def test_the_approval_rate_is_the_run_that_was_used(tmp_path: Path, films: Path) -> None:
    trial(tmp_path / "runs")
    result = film_result(FILM, tmp_path / "runs", films, old_minutes=240, new_minutes=60, old_way_source="stopwatch")
    assert result.used is not None and result.used.stamp == "20261102T040000Z"  # the last completed one
    assert (result.approval["overall"], result.approval["key_moments"]) == ("13/15", "8/9")


def test_time_saved_is_old_minus_new_with_run_log_time_beside_it(tmp_path: Path, films: Path) -> None:
    trial(tmp_path / "runs")
    result = film_result(FILM, tmp_path / "runs", films, old_minutes=240, new_minutes=60, old_way_source="stopwatch")
    assert result.time_saved == Decimal(180) and result.time_saved_share == Decimal("0.75")
    assert [r.minutes for r in result.runs] == [Decimal(5), None, Decimal(20), Decimal(75)]
    assert result.run_log_minutes == Decimal(100)


def test_the_page_holds_numbers_and_no_unreleased_text(tmp_path: Path, films: Path) -> None:
    trial(tmp_path / "runs")
    path = write_result(
        film_result(FILM, tmp_path / "runs", films, old_minutes=240, new_minutes=60, old_way_source="stopwatch"),
        tmp_path / "runs",
    )
    assert path == tmp_path / "runs" / FILM / "result.md"
    page = path.read_text(encoding="utf-8")
    for line in ("The Quiet River", "Time Saved: 180 min (75%)", "Approval Rate: 13/15 (87%)",
                 "Cost per Film: $1.3500 over 4 Runs"):  # fmt: skip
        assert line in page
    for secret in (POST, MOMENT, "drowns", str(tmp_path)):
        assert secret not in page
    data = json.loads((tmp_path / "runs" / FILM / "result.json").read_text(encoding="utf-8"))
    assert data["cost_usd"] == "1.35" and data["time_saved_minutes"] == "180" and data["approval_rate"] == "13/15"
    # T22: how many were approved over a flag, as counts: never which words or why.
    assert "Social Posts approved over a flag: 1" in page and "approved with no re-check: 0" in page
    assert data["approved_over_a_flag"] == 1 and data["approved_not_rechecked"] == 0
    assert "tiktok vi" not in page.split("## Runs")[0].split("over a flag")[1]


def test_a_trial_with_no_completed_run_says_it_is_not_finished(tmp_path: Path, films: Path) -> None:
    a_run(tmp_path / "runs", "20261102T010000Z", costs=["0.30"], end="error")
    result = film_result(FILM, tmp_path / "runs", films, old_minutes=240, new_minutes=60, old_way_source="stopwatch")
    assert result.used is None
    page = write_result(result, tmp_path / "runs").read_text(encoding="utf-8")
    assert "not finished" in page and "Cost per Film: $0.3000 over 1 Run" in page


@pytest.mark.parametrize(
    ("film", "old", "new", "message"),
    [
        ("Quiet River", 240, 60, "slug"),
        ("no-such-film", 240, 60, "film.toml"),
        (FILM, 0, 60, "old-way"),
        (FILM, 240, -1, "new-way"),
    ],
)
def test_bad_input_is_refused_before_anything_is_written(
    tmp_path: Path, films: Path, film: str, old: float, new: float, message: str
) -> None:
    trial(tmp_path / "runs")
    with pytest.raises((ValueError, FileNotFoundError), match=message):
        film_result(film, tmp_path / "runs", films, old_minutes=old, new_minutes=new, old_way_source="stopwatch")


def test_a_film_with_no_runs_is_refused(tmp_path: Path, films: Path) -> None:
    with pytest.raises(ValueError, match="no Run Logs"):
        film_result(FILM, tmp_path / "runs", films, old_minutes=240, new_minutes=60, old_way_source="stopwatch")


# From /code-review on #43.


def test_eval_set_and_dummy_runs_are_not_launch_work(tmp_path: Path, films: Path) -> None:
    runs = tmp_path / "runs"
    trial(runs)
    a_run(runs, "20261102T050000Z", costs=["0.60"], end="completed", extra=[{"event": "eval_run", "at": "x"}])
    a_run(runs, "20261102T060000Z", costs=["0.02"], end="completed", agent="dummy")
    result = film_result(FILM, runs, films, old_minutes=240, new_minutes=60, old_way_source="stopwatch")
    assert result.cost == Decimal("1.35") and len(result.runs) == 4  # as before: neither counts
    assert result.left_out == 2
    page = write_result(result, runs).read_text(encoding="utf-8")
    assert "2 Eval Set or dummy Runs are left out" in page


def test_a_completed_run_with_no_approved_posts_is_not_the_one_used(tmp_path: Path, films: Path) -> None:
    runs = tmp_path / "runs"
    trial(runs)
    a_run(runs, "20261102T050000Z", costs=["0.20"], end="completed", rate={**RATE, "social_posts": "0/6"})
    result = film_result(FILM, runs, films, old_minutes=240, new_minutes=60, old_way_source="stopwatch")
    assert result.used is not None and result.used.stamp == "20261102T040000Z"  # no Campaign came out of the last one


def test_the_run_that_was_used_can_be_named(tmp_path: Path, films: Path) -> None:
    runs = tmp_path / "runs"
    trial(runs)
    result = film_result(
        FILM, runs, films, old_minutes=240, new_minutes=60, old_way_source="stopwatch", used_run="20261102T030000Z"
    )
    assert result.used is not None and result.approval["overall"] == "9/15"
    with pytest.raises(ValueError, match="no completed Run"):
        film_result(
            FILM, runs, films, old_minutes=240, new_minutes=60, old_way_source="stopwatch", used_run="20261102T010000Z"
        )  # it failed


def test_the_run_used_is_the_last_to_finish_not_the_last_folder(tmp_path: Path, films: Path) -> None:
    runs = tmp_path / "runs"
    a_run(runs, "20261102T030000Z", costs=["0.40"], end="completed", minutes=120, rate=RATE)  # ends 05:00
    a_run(runs, "20261102T040000Z", costs=["0.40"], end="completed", minutes=10, rate={**RATE, "overall": "1/15"})
    a_run(runs, "20261102T030000Z-10", costs=["0.01"], end="error", minutes=1)
    a_run(runs, "20261102T030000Z-2", costs=["0.01"], end="error", minutes=1)
    result = film_result(FILM, runs, films, old_minutes=240, new_minutes=60, old_way_source="stopwatch")
    assert result.used is not None and result.used.stamp == "20261102T030000Z"
    assert [r.stamp for r in result.runs] == [
        "20261102T030000Z", "20261102T030000Z-2", "20261102T030000Z-10", "20261102T040000Z"]  # fmt: skip


def test_a_malformed_run_log_is_refused_with_a_message(tmp_path: Path, films: Path) -> None:
    folder = a_run(tmp_path / "runs", "20261102T030000Z", costs=["0.40"], end="completed", rate=RATE)
    log = folder / "run.jsonl"
    log.write_text(log.read_text().replace('"total_usd": "0.40"', '"total_usd": "lots"') + "[1, 2]\n")
    with pytest.raises(ValueError, match="20261102T030000Z"):
        film_result(FILM, tmp_path / "runs", films, old_minutes=240, new_minutes=60, old_way_source="stopwatch")


def test_a_slower_new_way_says_so(tmp_path: Path, films: Path) -> None:
    trial(tmp_path / "runs")
    result = film_result(FILM, tmp_path / "runs", films, old_minutes=60, new_minutes=90, old_way_source="stopwatch")
    page = write_result(result, tmp_path / "runs").read_text(encoding="utf-8")
    assert "Time Saved: none: Trailer-to-Campaign took 30 min longer (50%)" in page


def test_a_run_from_before_the_flag_count_says_not_recorded(tmp_path: Path, films: Path) -> None:
    runs = tmp_path / "runs"
    old = {k: v for k, v in RATE.items() if k not in ("approved_over_a_flag", "approved_not_rechecked")}
    a_run(runs, "20261102T040000Z", costs=["0.45"], end="completed", minutes=75, rate=old)
    path = write_result(
        film_result(FILM, runs, films, old_minutes=240, new_minutes=60, old_way_source="stopwatch"), runs
    )

    page = path.read_text(encoding="utf-8")
    assert "Social Posts approved over a flag: not recorded" in page  # unknown is not zero
    data = json.loads(path.with_name("result.json").read_text(encoding="utf-8"))
    assert data["approved_over_a_flag"] is None and data["approved_not_rechecked"] is None


def test_a_flag_count_that_is_not_a_list_reads_not_recorded(tmp_path: Path, films: Path) -> None:
    runs = tmp_path / "runs"
    odd = {**RATE, "approved_over_a_flag": None, "approved_not_rechecked": "x"}
    a_run(runs, "20261102T040000Z", costs=["0.45"], end="completed", minutes=75, rate=odd)
    path = write_result(
        film_result(FILM, runs, films, old_minutes=240, new_minutes=60, old_way_source="stopwatch"), runs
    )

    assert "approved over a flag: not recorded; approved with no re-check: not recorded" in path.read_text(
        encoding="utf-8"
    )


# T28: the page says how the old way was timed, and never guesses.

# The top of the page exactly as it was before T28, taken from `t2c result` on main.
STOPWATCH_PAGE_TOP = "\n".join(
    [
        "# The Quiet River: side by side",
        "",
        "- Time Saved: 180 min (75%)",
        "- Approval Rate: 13/15 (87%)",
        "- Cost per Film: $1.3500 over 4 Runs",
        "",
        "## How it was measured",
        "",
        "- The old way: 240 min of active work, timed by that marketer's stopwatch.",
        "- Trailer-to-Campaign: 60 min of active work, timed the same way.",
        "- Both started with the trailer link and the catalog facts, and stopped with the six Social Posts ready "
        "to publish (docs/side-by-side.md).",
        "- The Run Logs add up to 100.0 min from start to finish. That includes any wait for the Marketer at "
        "Approval, so it is a check on the stopwatch, not the measure.",
        "- Cost per Film is every Run's cost: the Film Budget caps each Run, not the Film, so re-runs and failed "
        "Runs count.",
        "",
        "## Approval Rate, from the Run that was used (20261102T040000Z)",
        "",
    ]
)


def page_for(tmp_path: Path, films: Path, source: str, *, old: int = 240, new: int = 60) -> str:
    runs = tmp_path / "runs"
    if not runs.exists():
        trial(runs)
    result = film_result(FILM, runs, films, old_minutes=old, new_minutes=new, old_way_source=source)
    return write_result(result, runs).read_text(encoding="utf-8")


def line_starting(page: str, start: str) -> str:
    return next(line for line in page.splitlines() if line.startswith(start))


def test_a_stopwatch_page_still_says_what_it_said(tmp_path: Path, films: Path) -> None:
    assert page_for(tmp_path, films, "stopwatch").startswith(STOPWATCH_PAGE_TOP)


def test_a_memory_page_makes_no_claim_that_the_old_way_was_timed(tmp_path: Path, films: Path) -> None:
    page = page_for(tmp_path, films, "memory")
    assert "stopwatch" not in page.lower()
    for claim in ("timed", "the same way", "How it was measured", "Both started", "not the measure"):
        assert claim not in page


def test_a_memory_page_says_the_old_way_is_from_memory(tmp_path: Path, films: Path) -> None:
    old_way = line_starting(page_for(tmp_path, films, "memory"), "- The old way:")
    assert "240 min" in old_way and "from memory" in old_way
    assert "active work" not in old_way  # nobody recorded what the minutes held


def test_a_memory_page_makes_no_claim_about_how_the_new_way_was_timed(tmp_path: Path, films: Path) -> None:
    new_way = line_starting(page_for(tmp_path, films, "memory"), "- Trailer-to-Campaign:")
    assert "60 min" in new_way and "as that marketer gave it" in new_way  # the flag is only about the old way


def test_a_memory_page_says_the_run_logs_check_only_the_new_way(tmp_path: Path, films: Path) -> None:
    run_logs = line_starting(page_for(tmp_path, films, "memory"), "- The Run Logs")
    assert "100.0 min" in run_logs and "Nothing here checks the old way's" in run_logs


def test_a_memory_page_labels_time_saved_an_estimate(tmp_path: Path, films: Path) -> None:
    page = page_for(tmp_path, films, "memory")
    assert "- Time Saved (estimate): 180 min (75%)" in page
    assert "Time Saved: " not in page


def test_a_stopwatch_page_does_not_call_time_saved_an_estimate(tmp_path: Path, films: Path) -> None:
    page = page_for(tmp_path, films, "stopwatch")
    assert "estimate" not in page and "from memory" not in page


def test_a_slower_new_way_from_memory_is_still_an_estimate(tmp_path: Path, films: Path) -> None:
    page = page_for(tmp_path, films, "memory", old=60, new=90)
    assert "- Time Saved (estimate): none: Trailer-to-Campaign took 30 min longer (50%)" in page


def test_memory_changes_only_what_is_said_about_time(tmp_path: Path, films: Path) -> None:
    stopwatch, memory = page_for(tmp_path, films, "stopwatch"), page_for(tmp_path, films, "memory")
    assert line_starting(memory, "- Cost per Film is") == line_starting(stopwatch, "- Cost per Film is")
    assert memory.split("## Approval Rate")[1] == stopwatch.split("## Approval Rate")[1]
    for start in ("# The Quiet River", "- Approval Rate:", "- Cost per Film:"):
        assert line_starting(memory, start) == line_starting(stopwatch, start)


@pytest.mark.parametrize(("source", "estimate"), [("stopwatch", False), ("memory", True)])
def test_the_json_says_how_the_old_way_was_timed(tmp_path: Path, films: Path, source: str, estimate: bool) -> None:
    page_for(tmp_path, films, source)
    data = json.loads((tmp_path / "runs" / FILM / "result.json").read_text(encoding="utf-8"))
    assert data["old_way_source"] == source and data["time_saved_is_estimate"] is estimate
    assert data["old_way_minutes"] == "240" and data["time_saved_minutes"] == "180"


@pytest.mark.parametrize("source", ["", "guess", "Memory", "stopwatch ", None])
def test_an_unknown_old_way_source_is_refused_before_anything_is_written(
    tmp_path: Path, films: Path, source: Any
) -> None:
    trial(tmp_path / "runs")
    with pytest.raises(ValueError, match="--old-way-source must be stopwatch or memory"):
        film_result(FILM, tmp_path / "runs", films, old_minutes=240, new_minutes=60, old_way_source=source)
    assert not (tmp_path / "runs" / FILM / "result.md").exists()


@pytest.mark.parametrize("source", ["", "guess", "Memory", "memory ", "stopwatch ", None])
def test_a_film_result_with_an_unknown_old_way_source_cannot_be_built(source: Any) -> None:
    with pytest.raises(ValueError, match="--old-way-source must be stopwatch or memory"):
        FilmResult("f", "T", Decimal(240), Decimal(60), source)


@pytest.mark.parametrize(("source", "estimate"), [("stopwatch", False), ("memory", True)])
def test_a_film_result_with_a_known_old_way_source_can_be_built(source: str, estimate: bool) -> None:
    result = FilmResult("f", "T", Decimal(240), Decimal(60), source)
    assert result.old_way_source == source and result.time_saved_is_estimate is estimate


def test_a_film_result_has_no_default_old_way_source() -> None:
    with pytest.raises(TypeError, match="old_way_source"):
        FilmResult("f", "T", Decimal(240), Decimal(60))  # type: ignore[call-arg]


def test_the_old_way_source_has_no_default(tmp_path: Path, films: Path) -> None:
    trial(tmp_path / "runs")
    with pytest.raises(TypeError, match="old_way_source"):
        film_result(FILM, tmp_path / "runs", films, old_minutes=240, new_minutes=60)  # type: ignore[call-arg]
