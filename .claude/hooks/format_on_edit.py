#!/usr/bin/env python3
"""PostToolUse hook: format a Python file right after Claude edits or writes it.

Runs the project's own ruff (`.venv/bin/ruff` in the file's repo, the version in uv.lock),
so the formatting matches CI's `uv run ruff format --check` (T31; it used `uvx`, which
fetched whatever ruff was newest). Without that ruff, for example before `uv sync` in a
new worktree, it does nothing; lint_on_stop.py says so when Claude finishes. A file in
another repo (a second working folder) is left alone: that repo's .venv isn't ours to run.
Never blocks: always exits 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def git_line(folder: str, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", folder, *args], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def project_ruff(path: str) -> str:
    """The ruff in the file's repo, if that repo is this project or one of its worktrees (one git common dir)."""
    folder = os.path.dirname(path) or "."
    top = git_line(folder, "rev-parse", "--show-toplevel")
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if top and project:
        ours = git_line(project, "rev-parse", "--path-format=absolute", "--git-common-dir")
        theirs = git_line(top, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if not ours or os.path.realpath(ours) != os.path.realpath(theirs):
            return ""
    ruff = os.path.join(top, ".venv", "bin", "ruff") if top else ""
    return ruff if ruff and os.access(ruff, os.X_OK) else ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not path.endswith(".py") or not os.path.isfile(path):
        return 0
    ruff = project_ruff(path)
    if not ruff:
        return 0
    try:
        subprocess.run(
            [ruff, "format", "--quiet", path], capture_output=True, timeout=40
        )  # 3 git calls at 5s + 40s: inside the 60s limit
    except (OSError, subprocess.TimeoutExpired):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
