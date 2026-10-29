"""Tests for the Claude Code hooks in .claude/hooks/.

Each hook reads a tool call as JSON on stdin, the way Claude Code sends it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / ".claude" / "hooks"


def run_hook(name: str, tool_input: dict, cwd: Path | None = None) -> subprocess.CompletedProcess:
    payload = {"tool_input": tool_input, "cwd": str(cwd) if cwd else None}
    return subprocess.run(
        [sys.executable, str(HOOKS / name)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo on a feature branch."""
    subprocess.run(["git", "init", "-q", "-b", "t1-skeleton", str(tmp_path)], check=True)
    return tmp_path


def git_switch_main(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "main"], check=True)


# --- block_forbidden.py -------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf build",
        "cd src && rm -r -f cache",
        "git push --force origin t1-skeleton",
        "git push origin HEAD:refs/heads/main",
        "git push origin +t1-skeleton",
        "git reset --hard HEAD~1",
        "git add .env",
        "git add config/.env.local",
        "bash -c 'git push -f'",
    ],
)
def test_blocks_forbidden_commands(repo: Path, command: str) -> None:
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 2, result.stderr
    assert "Blocked by team policy" in result.stderr


@pytest.mark.parametrize(
    "command",
    [
        "git push -u origin t1-skeleton",
        "git push --force-with-lease origin t1-skeleton",
        "git add .env.example",
        "git add src/ tests/",
        "rm -r old_frames",
        "uv run pytest -q",
    ],
)
def test_allows_normal_commands(repo: Path, command: str) -> None:
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 0, result.stderr
    assert "permissionDecision" not in result.stdout


def test_blocks_commit_on_main(repo: Path) -> None:
    git_switch_main(repo)
    result = run_hook("block_forbidden.py", {"command": "git commit -m wip"}, repo)
    assert result.returncode == 2


def test_blocks_switching_to_main_then_committing(repo: Path) -> None:
    result = run_hook("block_forbidden.py", {"command": "git checkout main && git commit -m x"}, repo)
    assert result.returncode == 2


def test_asks_before_adding_a_dependency(repo: Path) -> None:
    result = run_hook("block_forbidden.py", {"command": "uv add anthropic"}, repo)
    assert result.returncode == 0
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "ask"


def test_ignores_malformed_input() -> None:
    result = subprocess.run(
        [sys.executable, str(HOOKS / "block_forbidden.py")], input="not json", capture_output=True, text=True
    )
    assert result.returncode == 0


# --- prompt_changed.py --------------------------------------------------------


def test_reminds_to_run_eval_set_after_prompt_edit() -> None:
    result = run_hook("prompt_changed.py", {"file_path": "/repo/src/agents/checker/prompts/system.md"})
    assert result.returncode == 0
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Eval Set" in context


def test_stays_quiet_for_other_files() -> None:
    result = run_hook("prompt_changed.py", {"file_path": "/repo/src/agents/checker/agent.py"})
    assert result.returncode == 0
    assert result.stdout == ""


# --- format_on_edit.py --------------------------------------------------------


def test_format_hook_never_blocks_on_non_python_files() -> None:
    result = run_hook("format_on_edit.py", {"file_path": "/repo/README.md"})
    assert result.returncode == 0


# --- settings.json ------------------------------------------------------------


def test_settings_wire_every_hook_file() -> None:
    settings = (HOOKS.parent / "settings.json").read_text()
    for hook in HOOKS.glob("*.py"):
        assert hook.name in settings, f"{hook.name} exists but is not wired in settings.json"
