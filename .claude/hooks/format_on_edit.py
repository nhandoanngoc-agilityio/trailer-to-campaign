#!/usr/bin/env python3
"""PostToolUse hook: format a Python file right after Claude edits or writes it.

Runs `uvx ruff format` on the edited file. uvx runs ruff in a throwaway env,
so nothing is added to pyproject.toml. Never blocks: always exits 0.
"""

import json
import shutil
import subprocess
import sys


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not path.endswith(".py") or not shutil.which("uvx"):
        return 0
    subprocess.run(["uvx", "ruff", "format", "--quiet", path], capture_output=True, timeout=60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
