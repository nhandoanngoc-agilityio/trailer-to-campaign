"""Tests for the Claude Code hooks in .claude/hooks/.

Each hook reads a tool call as JSON on stdin, the way Claude Code sends it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / ".claude" / "hooks"


def hook_python() -> str:
    """The `python3` .claude/settings.json would find: not this project's virtualenv, which `uv run` puts first (T24).

    T2C_HOOK_PYTHON names another one, so CI can test the oldest we expect.
    """
    if override := os.environ.get("T2C_HOOK_PYTHON"):
        return override
    venv_bin = (Path(sys.prefix) / "bin").resolve()
    path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if Path(p).resolve() != venv_bin)
    found = shutil.which("python3", path=path)
    if not found:  # falling back to the project's Python would hide the very thing these tests are for
        raise RuntimeError("no python3 outside the virtualenv: set T2C_HOOK_PYTHON to test the hooks")
    return found


def run_hook(name: str, tool_input: dict, cwd: Path | None = None) -> subprocess.CompletedProcess:
    payload = {"tool_input": tool_input, "cwd": str(cwd) if cwd else None}
    return subprocess.run(
        [hook_python(), str(HOOKS / name)],
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
        [hook_python(), str(HOOKS / "block_forbidden.py")], input="not json", capture_output=True, text=True
    )
    assert result.returncode == 0


def test_a_crash_while_judging_blocks_the_command() -> None:
    """Exit 1 lets a command run, so a guard that crashes would wave everything through (T24)."""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(HOOKS)!r})\n"
        "import block_forbidden as hook\n"
        "def crash(*args, **kwargs):\n"
        "    raise TypeError('zip() takes no keyword arguments')\n"
        "hook.find_violation = crash\n"
        "sys.exit(hook.main())\n"
    )
    result = subprocess.run(
        [hook_python(), "-B", "-c", script],
        input=json.dumps({"tool_input": {"command": "git status"}}),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "wasn't checked" in result.stderr
    assert "ask" in result.stderr  # a block means stop and ask, not edit the guard (CLAUDE.md)


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        "{}",
        "null",
        '{"tool_input": null}',
        '{"tool_input": {}}',
        '{"tool_input": {"cmd": "git push -f"}}',
        '{"tool_input": {"command": 1}}',
    ],
)
def test_a_payload_with_no_command_blocks(payload: str) -> None:
    """This hook only sees Bash calls, so no command means Claude Code's payload changed (T24)."""
    result = subprocess.run(
        [hook_python(), str(HOOKS / "block_forbidden.py")], input=payload, capture_output=True, text=True
    )
    assert result.returncode == 2


@pytest.mark.parametrize(
    "command",
    [
        'echo "\\""|git reset --hard',
        'echo "\\""||git push --force origin main',
        'echo "\\""&git reset --hard',
        '(git reset --hard;echo "\\"")',
        'echo "\\"">evals/expected/x.toml',
        '(echo "\\"")>evals/expected/x.toml',  # ")>" is a separator and a redirect, not one operator
        "(echo hi)>evals/expected/x.toml",  # the same, where shlex can read it
        "(echo hi)>>evals/expected/x.toml",
        'bash -c "git reset --hard"; echo "\\""',  # a quoted -c payload stays one word (security review)
        "cat <(git reset --hard)",
        "echo >(git reset --hard)",
        "echo hi;>evals/expected/x.toml",
        "echo hi|>evals/expected/x.toml",
        "git reset --hard # a comment with a stray ' quote",
        "echo $'\\'' ; git reset --hard",  # \' doesn't end a $'...' string (T24 @claude)
        "echo $'\\'' > evals/expected/x.toml",
        "echo 'never closed ; git reset --hard",  # a quote left open: the command can't be read, so it isn't checked
        'echo "never closed ; git reset --hard',
        "echo $'never closed ; git reset --hard",
        "echo hi >! evals/expected/x.toml",  # zsh's clobbering redirects, and zsh is the Mac's shell (security review)
        "echo hi >>! evals/expected/x.toml",
        "echo hi &>! evals/expected/x.toml",
        "echo a\r# ; git reset --hard",  # a shell reads \r as part of a word, so # starts no comment
        "echo ${x:-a # b}; git reset --hard",  # nor does # inside ${...}
    ],
)
def test_operators_still_split_a_command_shlex_cannot_read(repo: Path, command: str) -> None:
    """A shell reads "\\"" fine, but shlex raises, and the fallback must not hide what follows (T24 /code-review)."""
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 2, result.stderr


@pytest.mark.parametrize(
    "command",
    [
        'echo "a | git reset --hard" "\\""',  # operators inside quotes are text
        "echo 'a; git reset --hard'",
        'grep "\\"" notes.md',
        "echo $'a; git reset --hard\\n'",
        "echo $'it\\'s'",
        "echo git\\ reset\\ --hard",
        "echo hi # git reset --hard",
    ],
)
def test_quoted_text_is_not_a_command(repo: Path, command: str) -> None:
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 0, result.stderr


def test_a_hung_git_blocks_the_command(tmp_path: Path) -> None:
    """A hung git isn't "not on main": it means the branch is unknown, so the command wasn't checked (T24)."""
    fake_git = tmp_path / "git"
    fake_git.write_text("#!/bin/sh\nexec sleep 8\n")
    fake_git.chmod(0o755)
    result = subprocess.run(
        [hook_python(), str(HOOKS / "block_forbidden.py")],
        input=json.dumps({"tool_input": {"command": "git commit -m x"}, "cwd": str(tmp_path)}),
        capture_output=True,
        text=True,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
    )
    assert result.returncode == 2
    assert "wasn't checked" in result.stderr


def test_a_folder_the_command_has_yet_to_make_is_not_a_failure(tmp_path: Path) -> None:
    result = run_hook("block_forbidden.py", {"command": "mkdir new && cd new && git status"}, tmp_path)
    assert result.returncode == 0, result.stderr


def wired_hook_command() -> str:
    settings = json.loads((HOOKS.parent / "settings.json").read_text())
    bash = next(entry for entry in settings["hooks"]["PreToolUse"] if entry["matcher"] == "Bash")
    return next(hook["command"] for hook in bash["hooks"] if "block_forbidden.py" in hook["command"])


def test_the_wiring_tests_run_on_the_python_under_test() -> None:
    """They find `python3` beside hook_python(), which uv names python3.9: if that were another version, they'd lie."""
    env = {"PATH": f"{Path(hook_python()).parent}:/usr/bin:/bin"}
    found = subprocess.run(["/bin/sh", "-c", "python3 --version"], capture_output=True, text=True, env=env)
    expected = subprocess.run([hook_python(), "--version"], capture_output=True, text=True)
    assert found.stdout == expected.stdout


def test_the_wiring_blocks_when_the_hook_exits_with_anything_else(tmp_path: Path) -> None:
    """A syntax error or an older Python never reaches main(), so the settings must turn exit 1 into a block (T24)."""
    hooks = tmp_path / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "block_forbidden.py").write_text("import sys\nsys.exit(1)\n")
    python_dir = Path(hook_python()).parent
    env = {"PATH": f"{python_dir}:/usr/bin:/bin", "CLAUDE_PROJECT_DIR": str(tmp_path)}
    result = subprocess.run(
        ["/bin/sh", "-c", wired_hook_command()], input="{}", text=True, capture_output=True, env=env
    )
    assert result.returncode == 2


def test_the_wiring_blocks_when_there_is_no_python3(tmp_path: Path) -> None:
    env = {"PATH": str(tmp_path), "CLAUDE_PROJECT_DIR": str(tmp_path)}
    result = subprocess.run(
        ["/bin/sh", "-c", wired_hook_command()], input="{}", text=True, capture_output=True, env=env
    )
    assert result.returncode == 2


def test_the_wiring_lets_an_allowed_command_through(repo: Path) -> None:
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(HOOKS.parent.parent),
        "PATH": f"{Path(hook_python()).parent}:/usr/bin:/bin",
    }
    payload = json.dumps({"tool_input": {"command": "git status"}, "cwd": str(repo)})
    result = subprocess.run(
        ["/bin/sh", "-c", wired_hook_command()], input=payload, text=True, capture_output=True, env=env
    )
    assert result.returncode == 0, result.stderr


def test_ruff_judges_the_hooks_as_python_39() -> None:
    """Ruff's B905 asks for zip(strict=True), which is how 3.10-only code got into a hook (T24)."""
    source = "words = [1]\nraw = [2]\npairs = list(zip(words, raw))\n"
    project = HOOKS.parent.parent
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "B905",
            "--stdin-filename",
            str(HOOKS / "any_hook.py"),
            "-",
        ],
        input=source,
        capture_output=True,
        text=True,
        cwd=project,
    )
    assert result.returncode == 0, result.stdout


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


# --- .githooks/pre-commit (T14): an Answer Key change needs a person to say so -------

GITHOOKS = HOOKS.parent.parent / ".githooks"


def commit(repo: Path, path: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    import os

    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", path], check=True)
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "x"],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


@pytest.fixture
def hooked(repo: Path) -> Path:
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", str(GITHOOKS)], check=True)
    return repo


def test_pre_commit_refuses_an_answer_key_change(hooked: Path) -> None:
    result = commit(hooked, "evals/expected/charge.toml")
    assert result.returncode != 0
    assert "Answer Key" in result.stderr and "T2C_ANSWER_KEY_EDIT=1" in result.stderr


def test_pre_commit_lets_a_person_change_the_answer_key_on_purpose(hooked: Path) -> None:
    assert commit(hooked, "evals/expected/charge.toml", {"T2C_ANSWER_KEY_EDIT": "1"}).returncode == 0


def test_pre_commit_lets_everything_else_through(hooked: Path) -> None:
    assert commit(hooked, "evals/films/charge/film.toml").returncode == 0


@pytest.mark.parametrize(
    "command",
    [
        "T2C_ANSWER_KEY_EDIT=1 git commit -m x",
        "export T2C_ANSWER_KEY_EDIT=1 && git commit -m x",
        "env T2C_ANSWER_KEY_EDIT=1 git commit -m x",
    ],
)
def test_the_answer_key_override_is_for_people_only(repo: Path, command: str) -> None:
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 2 and "T2C_ANSWER_KEY_EDIT" in result.stderr
