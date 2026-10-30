"""Tests for the side-by-side result (T12): Time Saved, Approval Rate and cost per Film, from a Film's own records."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trailer_to_campaign.result import film_result, write_result

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
) -> Path:
    """One Run Log under runs/<film>/<stamp>/, as the pipeline writes it; end=None is a Run killed mid-way."""
    folder = runs / FILM / stamp
    folder.mkdir(parents=True)
    began = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    start = began.isoformat()
    lines: list[dict[str, Any]] = [{"event": "run_started", "at": start, "film": FILM}]
    lines += [{"event": "model_call", "at": start, "agent": "watcher", "cost_usd": c} for c in costs]
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
    result = film_result(FILM, tmp_path / "runs", films, old_minutes=240, new_minutes=60)
    assert result.cost == Decimal("1.35")  # 0.30 + 0.15 + 0.40 + 0.50
    assert [r.ended for r in result.runs] == ["error", "did not finish", "completed", "completed"]
    assert result.runs[1].cost == Decimal("0.15")  # no run_finished: its model calls, which were spent


def test_the_approval_rate_is_the_run_that_was_used(tmp_path: Path, films: Path) -> None:
    trial(tmp_path / "runs")
    result = film_result(FILM, tmp_path / "runs", films, old_minutes=240, new_minutes=60)
    assert result.used is not None and result.used.stamp == "20261102T040000Z"  # the last completed one
    assert (result.approval["overall"], result.approval["key_moments"]) == ("13/15", "8/9")


def test_time_saved_is_old_minus_new_with_run_log_time_beside_it(tmp_path: Path, films: Path) -> None:
    trial(tmp_path / "runs")
    result = film_result(FILM, tmp_path / "runs", films, old_minutes=240, new_minutes=60)
    assert result.time_saved == Decimal(180) and result.time_saved_share == Decimal("0.75")
    assert [r.minutes for r in result.runs] == [Decimal(5), None, Decimal(20), Decimal(75)]
    assert result.run_log_minutes == Decimal(100)


def test_the_page_holds_numbers_and_no_unreleased_text(tmp_path: Path, films: Path) -> None:
    trial(tmp_path / "runs")
    path = write_result(film_result(FILM, tmp_path / "runs", films, old_minutes=240, new_minutes=60), tmp_path / "runs")
    assert path == tmp_path / "runs" / FILM / "result.md"
    page = path.read_text(encoding="utf-8")
    for line in ("The Quiet River", "Time Saved: 180 min (75%)", "Approval Rate: 13/15 (87%)",
                 "Cost per Film: $1.3500 over 4 Runs"):  # fmt: skip
        assert line in page
    for secret in (POST, MOMENT, "drowns", str(tmp_path)):
        assert secret not in page
    data = json.loads((tmp_path / "runs" / FILM / "result.json").read_text(encoding="utf-8"))
    assert data["cost_usd"] == "1.35" and data["time_saved_minutes"] == "180" and data["approval_rate"] == "13/15"


def test_a_trial_with_no_completed_run_says_it_is_not_finished(tmp_path: Path, films: Path) -> None:
    a_run(tmp_path / "runs", "20261102T010000Z", costs=["0.30"], end="error")
    result = film_result(FILM, tmp_path / "runs", films, old_minutes=240, new_minutes=60)
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
        film_result(film, tmp_path / "runs", films, old_minutes=old, new_minutes=new)


def test_a_film_with_no_runs_is_refused(tmp_path: Path, films: Path) -> None:
    with pytest.raises(ValueError, match="no Run Logs"):
        film_result(FILM, tmp_path / "runs", films, old_minutes=240, new_minutes=60)
