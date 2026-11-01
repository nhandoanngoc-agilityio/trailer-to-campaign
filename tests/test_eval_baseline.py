"""Tests for what an Eval Set report was measured on, and for finding `main`'s report again (T20)."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from trailer_to_campaign.eval_baseline import agents_hash, agents_hash_at, current_commit, find_baseline
from trailer_to_campaign.evals import show_baseline


def a_package(root: Path) -> Path:
    package = root / "src" / "trailer_to_campaign"
    for name, text in {
        "agents/checker.py": 'MODEL = "a"\n',
        "agents/prompts/checker.md": "Check it.\n",
        "tools/catalog.py": "tool = 1\n",
        "footage/frames.py": "frames = 1\n",
        "evals.py": "score = 1\n",
    }.items():
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return package


@pytest.mark.parametrize(
    "changed", ["agents/prompts/checker.md", "agents/checker.py", "tools/catalog.py", "footage/frames.py"]
)
def test_the_hash_changes_with_an_agents_prompt_model_or_tool(tmp_path: Path, changed: str) -> None:
    package = a_package(tmp_path)
    before = agents_hash(package)
    (package / changed).write_text("changed\n", encoding="utf-8")
    assert agents_hash(package) != before


def test_a_new_prompt_file_changes_the_hash(tmp_path: Path) -> None:
    package = a_package(tmp_path)
    before = agents_hash(package)
    (package / "agents" / "prompts" / "new.md").write_text("New.\n", encoding="utf-8")
    assert agents_hash(package) != before


def test_the_hash_does_not_change_otherwise(tmp_path: Path) -> None:
    package = a_package(tmp_path)
    before = agents_hash(package)
    (package / "evals.py").write_text("score = 2\n", encoding="utf-8")
    (package / "README.md").write_text("Docs.\n", encoding="utf-8")
    (package / "agents" / "__pycache__").mkdir()
    (package / "agents" / "__pycache__" / "checker.pyc").write_bytes(b"\x00")
    (package / "agents" / "__pycache__" / "stray.py").write_text("no\n", encoding="utf-8")
    (package / "agents" / "prompts" / ".DS_Store").write_bytes(b"\x00")
    assert agents_hash(package) == before


def git(cwd: Path, *args: str) -> str:
    identity = ["-c", "user.name=T", "-c", "user.email=t@example.org", "-c", "commit.gpgsign=false"]
    done = subprocess.run(["git", *identity, *args], cwd=cwd, check=True, capture_output=True, text=True)
    return done.stdout.strip()


def test_the_hash_of_a_commit_equals_the_hash_of_its_files(tmp_path: Path) -> None:
    package = a_package(tmp_path)
    (package / "agents" / "prompts" / "việt.md").write_text("Tên có dấu.\n", encoding="utf-8")  # git quotes such paths
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "one")
    committed = agents_hash(package)
    assert agents_hash_at("HEAD", package) == committed

    (package / "agents" / "prompts" / "checker.md").write_text("Changed.\n", encoding="utf-8")
    assert agents_hash(package) != committed  # the branch moved on
    assert agents_hash_at("HEAD", package) == committed  # and the commit's hash can still be found
    assert current_commit(package) == git(tmp_path, "rev-parse", "HEAD")


def test_without_git_or_the_ref_the_answer_is_none(tmp_path: Path) -> None:
    package = a_package(tmp_path)
    assert agents_hash_at("main", package) is None
    assert current_commit(package) is None


HASH = "a" * 64


def report(reports: Path, name: str, **over: Any) -> Path:
    summary = {
        "at": "2026-11-01T00:00:00+00:00",
        "watcher": "v0",
        "mode": "full",
        "commit": "abc1234def",
        "agents_hash": HASH,
        "skipped": [],
        "films": [
            {"film": "alpha", "stop": "completed", "cost_usd": "0.40", "errors": []},
            {"film": "gamma", "stop": "skipped"},
        ],
        "totals": {
            "planted": {"caught": 2, "total": 2, "not_reviewed": 0},
            "false_alarms": {"count": 0, "total": 1},
            "input_traps": {"caught": 1, "total": 1},
            "cost_usd": "0.40",
            "watcher": {"cost_usd": "0.20", "grounding_rejections": 0, "key_moments": 5},
        },
        **over,
    }
    path = reports / name
    path.mkdir(parents=True)
    (path / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return path


def test_the_newest_matching_report_is_the_baseline(tmp_path: Path) -> None:
    report(tmp_path, "20261101T000000Z")
    newest = report(tmp_path, "20261102T000000Z")
    report(tmp_path, "20261103T000000Z", agents_hash="b" * 64)  # newer, but other agents

    found = find_baseline(tmp_path, HASH, "v0")
    assert found is not None and found[0] == newest and found[1]["commit"] == "abc1234def"


@pytest.mark.parametrize(
    "over",
    [
        {"agents_hash": "b" * 64},
        {"agents_hash": None},
        {"mode": "drafts"},
        {"mode": "reused_key_moments"},
        {"mode": None},
        {"watcher": "sdk"},
        {"interrupted": ["alpha"]},
        {"not_started": ["alpha"]},
        {"films": [{"film": "alpha", "stop": "error", "cost_usd": "0.04", "errors": ["credit balance too low"]}]},
        {"films": [{"film": "alpha", "stop": "film_budget_reached", "cost_usd": "2.00", "errors": ["Budget"]}]},
        {"films": [{"film": "alpha", "stop": "completed", "cost_usd": "0.40", "errors": ["Checker failed"]}]},
        {"films": [{"film": "gamma", "stop": "skipped"}]},  # nothing ran
    ],
)
def test_a_report_that_cannot_stand_for_main_is_not_a_baseline(tmp_path: Path, over: dict[str, Any]) -> None:
    report(tmp_path, "20261101T000000Z", **over)
    assert find_baseline(tmp_path, HASH, "v0") is None


def test_an_unreadable_report_is_passed_over(tmp_path: Path) -> None:
    good = report(tmp_path, "20261101T000000Z")
    bad = tmp_path / "20261102T000000Z"
    bad.mkdir()
    (bad / "summary.json").write_text("{ not json", encoding="utf-8")
    (tmp_path / "20261103T000000Z").mkdir()  # no summary at all
    report(tmp_path, "20261104T000000Z", films="not a list")

    found = find_baseline(tmp_path, HASH, "v0")
    assert found is not None and found[0] == good


def test_no_saved_reports_at_all_is_no_baseline(tmp_path: Path) -> None:
    assert find_baseline(tmp_path / "missing", HASH, "v0") is None


def test_show_baseline_prints_the_numbers_to_compare_with(tmp_path: Path) -> None:
    reports = tmp_path / "runs" / "_eval"
    path = report(reports, "20261101T000000Z")
    out = io.StringIO()

    assert show_baseline(tmp_path / "runs", "v0", out=out, main_hash=lambda: HASH) == 0
    text = out.getvalue()
    assert str(path) in text and "abc1234" in text
    assert "Planted Drafts: caught 2 of 2" in text and "$0.40" in text and "alpha" in text


def test_show_baseline_says_only_when_a_main_run_is_needed(tmp_path: Path) -> None:
    report(tmp_path / "runs" / "_eval", "20261101T000000Z", agents_hash="b" * 64)
    out = io.StringIO()

    assert show_baseline(tmp_path / "runs", "v0", out=out, main_hash=lambda: HASH) == 1
    assert "new run on main" in out.getvalue()


def test_show_baseline_says_when_it_could_not_read_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import trailer_to_campaign.evals as evals

    report(tmp_path / "runs" / "_eval", "20261101T000000Z")
    monkeypatch.setattr(evals, "agents_hash", lambda: HASH)
    out = io.StringIO()

    assert show_baseline(tmp_path / "runs", "v0", out=out, main_hash=lambda: None) == 0
    assert "this checkout" in out.getvalue()  # a fallback must not pass for main's numbers
