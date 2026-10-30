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
        "rm -r '' -f build",  # an empty argument is not a separator (found in T14)
        "git status\ngit push -f origin t1-skeleton",  # a newline separates commands (T14 /code-review)
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


# T14: the Answer Key (evals/expected/) is human-owned. A tripwire for obvious shell writes, not a sandbox.

ANSWER_KEY_WRITES = [
    "echo x > evals/expected/charge.toml",
    "echo x >evals/expected/charge.toml",
    "echo x >> evals/expected/charge.toml",
    "uv run t2c eval 2> evals/expected/log.txt",
    "uv run t2c eval &> evals/expected/log.txt",
    "cp notes.toml evals/expected/charge.toml",
    "cp notes.toml evals/expected/",
    "cp -t evals/expected notes.toml",
    "cp --target-directory=evals/expected notes.toml",
    "mv evals/expected/charge.toml /tmp/",
    "mv notes.toml evals/expected/",
    "cat notes.toml | tee evals/expected/charge.toml",
    "tee -a evals/expected/charge.toml < notes.toml",
    "rm evals/expected/charge.toml",
    "touch evals/expected/new.toml",
    "sed -i 's/a/b/' evals/expected/charge.toml",
    "sed -i.bak 's/a/b/' evals/expected/charge.toml",
    "sed -i '' 's/a/b/' evals/expected/charge.toml",
    "sed --in-place 's/a/b/' evals/expected/charge.toml",
    "git checkout -- evals/expected/charge.toml",
    "git checkout main evals/expected/",
    "git restore evals/expected/charge.toml",
    "git rm evals/expected/charge.toml",
    "git mv evals/expected/charge.toml evals/expected/old.toml",
    # the same writes, spelled differently
    "touch ./evals/expected/new.toml",
    "touch /Users/someone/trailer-to-campaign/evals/expected/new.toml",
    "cd evals/expected && touch new.toml",
    "cd evals && sed -i 's/a/b/' expected/charge.toml",
    "bash -c 'cp notes.toml evals/expected/charge.toml'",
    "sudo rm evals/expected/charge.toml",
    # from /code-review on #45
    "echo hi\ntouch evals/expected/x.toml",
    "echo hi>evals/expected/x.toml",
    "cat a>>evals/expected/b.toml",
    "echo x >| evals/expected/a.toml",
    "rm -r evals",
    "git checkout -- evals",
    "git checkout .",
    "git restore .",
    "mv evals evals_old",
    "sed -Ei 's/a/b/' evals/expected/x.toml",
    "git -C evals checkout -- expected/a.toml",
    "cp -at evals/expected notes.toml",
    "(cd evals/expected && touch x.toml)",
    "cat > evals/expected/x.toml <<'EOF'\nfilms = []\nEOF",
    "cat <<'EOF' > notes.md\nhi\nEOF\ntouch evals/expected/x.toml",  # a command after the heredoc still counts
]

ANSWER_KEY_READS = [
    "cat evals/expected/charge.toml",
    "ls evals/expected/",
    "diff evals/expected/charge.toml /tmp/charge.toml",
    "grep spoiler evals/expected/charge.toml",
    "cp evals/expected/charge.toml /tmp/",
    "sed -n 1,5p evals/expected/charge.toml",
    "git diff -- evals/expected/",
    "git log -- evals/expected/",
    "git show HEAD:evals/expected/charge.toml > /tmp/charge.toml",
    "echo hi > evals/drafts/charge.toml",
    "cd evals/expected && cat charge.toml",
    # from /code-review on #45
    'grep ">" evals/expected/charge.toml',
    "bash -c 'cd evals/expected' && touch notes.toml",  # bash -c's cd ends with that shell
    "git checkout main",
    "git checkout -b t14-guard",
    "uv run t2c eval 2>&1 | tee /tmp/eval.log",
    "cp notes.toml backup.toml 2> /tmp/err.log",
    # a heredoc's body is text, not commands, even now that newlines separate commands
    "cat > notes.md <<'EOF'\nNever run git push --force or rm -rf build.\nEOF",
    "cat >> notes.md <<EOF\ntouch evals/expected/x.toml\nEOF\necho done",
]


@pytest.mark.parametrize("command", ANSWER_KEY_WRITES)
def test_blocks_shell_writes_to_the_answer_key(repo: Path, command: str) -> None:
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 2, result.stderr
    assert "Answer Key" in result.stderr


@pytest.mark.parametrize("command", ANSWER_KEY_READS)
def test_allows_reading_the_answer_key(repo: Path, command: str) -> None:
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("command", ["touch expected/x.toml", "sed -i 's/a/b/' expected/charge.toml", "rm -r expected"])
def test_a_shell_already_inside_evals_is_still_guarded(repo: Path, command: str) -> None:
    # Claude's Bash keeps its directory between calls: an earlier `cd evals` leaves the shell there.
    result = run_hook("block_forbidden.py", {"command": command}, repo / "evals")
    assert result.returncode == 2, result.stderr
    assert "Answer Key" in result.stderr


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
