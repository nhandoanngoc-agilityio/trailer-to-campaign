"""Tests for the per-run JSONL log."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trailer_to_campaign.run_log import RunLog

NOON = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_run_dir_is_film_then_utc_timestamp(tmp_path: Path) -> None:
    log = RunLog.create(tmp_path, "the-quiet-river", now=NOON)
    assert log.path == tmp_path / "the-quiet-river" / "20261029T120000Z" / "run.jsonl"


def test_two_runs_in_the_same_second_get_separate_dirs(tmp_path: Path) -> None:
    first = RunLog.create(tmp_path, "sample", now=NOON)
    second = RunLog.create(tmp_path, "sample", now=NOON)
    assert first.path != second.path


def test_each_event_is_one_json_line_written_immediately(tmp_path: Path) -> None:
    log = RunLog.create(tmp_path, "sample", now=NOON)
    log.event("model_call", model="claude-opus-5-5", cost_usd=Decimal("0.0123"))
    lines = read_lines(log.path)  # read before finish(): nothing is buffered
    assert lines[-1]["event"] == "model_call"
    assert lines[-1]["cost_usd"] == "0.0123"
    assert "at" in lines[-1]


def test_finish_writes_total_and_stop_reason(tmp_path: Path) -> None:
    log = RunLog.create(tmp_path, "sample", now=NOON)
    log.finish(total_usd=Decimal("1.5"), stop_reason="completed")
    last = read_lines(log.path)[-1]
    assert last == {**last, "event": "run_finished", "total_usd": "1.5", "stop_reason": "completed"}


def test_run_started_is_the_first_line(tmp_path: Path) -> None:
    log = RunLog.create(tmp_path, "sample", now=NOON)
    assert read_lines(log.path)[0]["event"] == "run_started"
    assert read_lines(log.path)[0]["film"] == "sample"


@pytest.mark.parametrize("film", ["../escape", "..", "A B", "", "a/b", "-lead", "trail-", "x\nevil"])
def test_film_slug_must_stay_inside_runs_dir(tmp_path: Path, film: str) -> None:
    with pytest.raises(ValueError, match="film"):
        RunLog.create(tmp_path, film, now=NOON)
    assert list(tmp_path.iterdir()) == []
