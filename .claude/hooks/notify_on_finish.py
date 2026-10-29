#!/usr/bin/env python3
"""Stop hook: show a desktop notification when Claude finishes its turn."""

import json
import platform
import shutil
import subprocess
import sys

TITLE = "Claude Code"
MESSAGE = "Finished in trailer-to-campaign. Your turn."


def main() -> int:
    try:
        json.load(sys.stdin)
    except json.JSONDecodeError:
        pass
    system = platform.system()
    if system == "Darwin":
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{MESSAGE}" with title "{TITLE}" sound name "Glass"',
            ],
            capture_output=True,
        )
    elif shutil.which("notify-send"):
        subprocess.run(["notify-send", TITLE, MESSAGE], capture_output=True)
    else:
        print("\a", end="", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
