#!/usr/bin/env python3
"""PostToolUse hook: when Claude edits an agent prompt, remind it that Verify must run the Eval Set.

A prompt change can pass every unit test and still let a spoiler through, so the
ticket loop (CLAUDE.md, step 4) requires an Eval Set comparison against main.
Prompts live in a `prompts/` folder next to the agent that uses them.

Never blocks: prints a JSON note that Claude sees as extra context, then exits 0.
"""

import json
import sys

NOTE = (
    "You changed an agent prompt ({path}). Before opening the PR, run the Eval Set "
    "and compare Traps caught and cost per Film against main (CLAUDE.md, ticket loop step 4). "
    "Fewer Traps caught blocks the PR. Tick 'Risky' in the PR if this prompt enforces a guardrail."
)


def is_prompt(path: str) -> bool:
    return "/prompts/" in path.replace("\\", "/")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    path = (payload.get("tool_input") or {}).get("file_path") or ""
    if is_prompt(path):
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": NOTE.format(path=path),
            }
        }
        print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
