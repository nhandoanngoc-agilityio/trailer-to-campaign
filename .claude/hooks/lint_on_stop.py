#!/usr/bin/env python3
"""Stop hook: Claude can't finish its turn while a changed Python file fails lint (T31).

Runs the project's own ruff (`.venv/bin/ruff`, the version in uv.lock) with `check` and
`format --check` on the .py files that changed in the working tree: tracked changes
against HEAD, and untracked files git doesn't ignore. That includes a person's own
unfinished files, since git can't tell who changed what; CI still lints everything.
Only this project and its worktrees: another repo the session cd'd into is left alone.

Exit 2 sends ruff's output back to Claude once. If lint still fails when Claude next
tries to stop (`stop_hook_active`), the turn ends with the failing files named: the file
may be one Claude must not or can't fix, and looping on it only spends tokens.

A lint gate, not a guard: when it can't judge (no ruff yet, a timeout, no git, bad
input) it lets the turn end and says so on stderr, rather than trapping every turn of a
session whose setup is broken. It never changes a file: `format --check`, never `format`.

Runs on the machine's python3, which is 3.9 on macOS, so it stays 3.9-compatible and
uses only the standard library (T24).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

DEFAULT_TIMEOUT = 30.0  # seconds per ruff call
GIT_TIMEOUT = 5.0  # seconds per git call; worst case 6 git + 2 ruff = 90s, inside settings.json's 100


def note(reason: str) -> int:
    print(f"lint_on_stop: lint not checked: {reason}", file=sys.stderr)
    return 0


def timeout() -> float:
    try:
        return float(os.environ.get("T2C_LINT_TIMEOUT", DEFAULT_TIMEOUT))  # lowered only by the tests
    except ValueError:
        return DEFAULT_TIMEOUT


def git(top: str, *args: str) -> list[str] | None:
    try:
        result = subprocess.run(["git", "-C", top, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    # -z output splits on NUL, so a name with spaces, quotes or non-ASCII letters arrives as it is
    separator = "\0" if "-z" in args else "\n"
    return [name for name in result.stdout.split(separator) if name]


def changed_python_files(top: str) -> list[str] | None:
    if git(top, "rev-parse", "--verify", "-q", "HEAD") is not None:
        tracked = git(top, "diff", "-z", "--name-only", "--diff-filter=d", "HEAD")
    else:  # no commit yet: only what's staged can be compared
        tracked = git(top, "diff", "-z", "--name-only", "--diff-filter=d", "--cached")
    untracked = git(top, "ls-files", "-z", "--others", "--exclude-standard")
    if tracked is None or untracked is None:
        return None
    names = sorted(set(tracked) | set(untracked))
    return [n for n in names if n.endswith(".py") and os.path.isfile(os.path.join(top, n))]


def same_project(top: str) -> bool | None:
    """Whether `top` is the project Claude Code opened, or one of its worktrees (one git common dir).

    None when git can't tell, which the caller reports rather than skipping silently.
    """
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project:
        return True  # run by hand, outside Claude Code: lint where it was pointed
    ours = git(project, "rev-parse", "--path-format=absolute", "--git-common-dir")
    theirs = git(top, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if ours is None or theirs is None:
        return None
    return os.path.realpath(ours[0]) == os.path.realpath(theirs[0])


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return note("the hook's input wasn't JSON")
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    if not cwd or not os.path.isdir(cwd):
        return note("no working folder in the hook's input")

    found = git(cwd, "rev-parse", "--show-toplevel")
    if not found:
        return 0  # not a git repo: nothing here is this project's code
    top = found[0]
    project = same_project(top)
    if project is None:
        return note("git couldn't tell whether this is the project's repo")
    if not project:
        return 0  # another repo the session cd'd into: its lint, and its .venv's ruff, aren't ours to run

    files = changed_python_files(top)
    if files is None:
        return note("git couldn't list the changed files")
    if not files:
        return 0

    ruff = os.path.join(top, ".venv", "bin", "ruff")
    if not os.access(ruff, os.X_OK):
        return note(f"{ruff} not found; run `uv sync`")

    failures = []
    for command in (["check"], ["format", "--check"]):  # --force-exclude: pyproject excludes hold, as in CI
        try:
            result = subprocess.run(
                [ruff, *command, "--force-exclude", "--", *files],
                cwd=top,
                capture_output=True,
                text=True,
                timeout=timeout(),
            )
        except subprocess.TimeoutExpired:
            return note(f"`ruff {' '.join(command)}` took longer than {timeout():g}s")
        except OSError as error:
            return note(f"ruff couldn't run: {error}")
        if result.returncode != 0:
            failures.append(f"$ ruff {' '.join(command)}\n{(result.stdout + result.stderr).strip()}")

    if not failures:
        return 0
    print("\n\n".join(failures), file=sys.stderr)
    if payload.get("stop_hook_active"):  # already sent back once: don't loop on a file that has to stay
        print(
            f"\nlint_on_stop: lint still fails (changed files: {', '.join(files)}). Ending the turn anyway; "
            "tell the person which files fail and why they were left.",
            file=sys.stderr,
        )
        return 0
    print(
        "\nLint fails on changed files. Fix them (`uv run ruff check --fix` and `uv run ruff format` "
        "on those files). If a file isn't yours to fix, leave it: the next stop ends the turn, "
        "and say why in your reply.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # a lint gate never traps a session on its own bug
        sys.exit(note(f"the hook failed: {error!r}"))
