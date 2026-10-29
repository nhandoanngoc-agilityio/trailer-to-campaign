"""Tests for the t2c command line."""

from __future__ import annotations

import json
import os
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from trailer_to_campaign.cli import EXIT_FILM_BUDGET_REACHED, main


def run_log_lines(runs_dir: Path) -> list[dict]:
    [path] = runs_dir.glob("sample/*/run.jsonl")
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_dummy_run_stops_at_a_tiny_budget(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["dummy-run", "--film", "sample", "--budget", "0.001", "--runs-dir", str(tmp_path)], env={})
    assert code == EXIT_FILM_BUDGET_REACHED
    assert "Film Budget reached: spent $" in capsys.readouterr().err
    last = run_log_lines(tmp_path)[-1]
    assert last["event"] == "run_finished"
    assert last["stop_reason"] == "film_budget_reached"
    assert Decimal(last["total_usd"]) <= Decimal("0.001")


def test_dummy_run_completes_within_a_normal_budget(tmp_path: Path) -> None:
    code = main(["dummy-run", "--film", "sample", "--calls", "3", "--runs-dir", str(tmp_path)], env={})
    assert code == 0
    lines = run_log_lines(tmp_path)
    calls = [line for line in lines if line["event"] == "model_call"]
    assert len(calls) == 3
    assert lines[-1]["stop_reason"] == "completed"
    assert Decimal(lines[-1]["total_usd"]) == sum(Decimal(c["cost_usd"]) for c in calls)


def test_budget_comes_from_the_environment(tmp_path: Path) -> None:
    code = main(["dummy-run", "--runs-dir", str(tmp_path)], env={"FILM_BUDGET_USD": "0.001"})
    assert code == EXIT_FILM_BUDGET_REACHED


def test_bad_budget_is_a_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as err:
        main(["dummy-run", "--budget", "-5", "--runs-dir", str(tmp_path)], env={})
    assert err.value.code == 2
    assert "--budget" in capsys.readouterr().err


def test_bad_budget_in_environment_is_a_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as err:
        main(["dummy-run", "--runs-dir", str(tmp_path)], env={"FILM_BUDGET_USD": "lots"})
    assert err.value.code == 2
    assert "FILM_BUDGET_USD" in capsys.readouterr().err


def test_t2c_is_installed_as_a_command(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    result = subprocess.run(
        ["t2c", "dummy-run", "--budget", "0.001", "--runs-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == EXIT_FILM_BUDGET_REACHED
    assert "Film Budget reached" in result.stderr
