#!/usr/bin/env python3
"""PostToolUse hook: format a Python file right after Claude edits or writes it.

Runs the project's own ruff (`.venv/bin/ruff` in the file's repo, the version in uv.lock),
so the formatting matches CI's `uv run ruff format --check` (T31; it used `uvx`, which
fetched whatever ruff was newest). Without that ruff, for example before `uv sync` in a
new worktree, it does nothing; lint_on_stop.py says so when Claude finishes.
Never blocks: always exits 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def project_ruff(path: str) -> str:
    try:
        top = subprocess.run(
            ["git", "-C", os.path.dirname(path) or ".", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
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
        subprocess.run([ruff, "format", "--quiet", path], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
