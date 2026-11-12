#!/usr/bin/env python3
"""Stop hook: Claude can't finish its turn while a Python file it changed fails lint (T31).

Runs the project's own ruff (`.venv/bin/ruff`, the version in uv.lock) with `check` and
`format --check` on the .py files that changed in the working tree: tracked changes
against HEAD, and untracked files git doesn't ignore. Only those files, so a stray
file elsewhere can't hold a session; CI still lints everything.

Exit 2 sends ruff's output back to Claude, which keeps working until lint passes (the
harness ends the turn after 8 blocks in a row anyway). Exit 0 lets the turn end.

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

DEFAULT_TIMEOUT = 30.0  # seconds per ruff call; settings.json gives the whole hook 70


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
        result = subprocess.run(["git", "-C", top, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line]


def changed_python_files(top: str) -> list[str] | None:
    if git(top, "rev-parse", "--verify", "-q", "HEAD") is not None:
        tracked = git(top, "diff", "--name-only", "--diff-filter=d", "HEAD")
    else:  # no commit yet: only what's staged can be compared
        tracked = git(top, "diff", "--name-only", "--diff-filter=d", "--cached")
    untracked = git(top, "ls-files", "--others", "--exclude-standard")
    if tracked is None or untracked is None:
        return None
    names = sorted(set(tracked) | set(untracked))
    return [n for n in names if n.endswith(".py") and os.path.isfile(os.path.join(top, n))]


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

    files = changed_python_files(top)
    if files is None:
        return note("git couldn't list the changed files")
    if not files:
        return 0

    ruff = os.path.join(top, ".venv", "bin", "ruff")
    if not os.access(ruff, os.X_OK):
        return note(f"{ruff} not found; run `uv sync`")

    failures = []
    for command in (["check"], ["format", "--check"]):
        try:
            result = subprocess.run(
                [ruff, *command, "--", *files], cwd=top, capture_output=True, text=True, timeout=timeout()
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
    print(
        "\nLint fails on files changed in this session. Fix them (`uv run ruff check --fix` and "
        "`uv run ruff format` on those files), or say why they must stay as they are.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # a lint gate never traps a session on its own bug
        sys.exit(note(f"the hook failed: {error!r}"))
