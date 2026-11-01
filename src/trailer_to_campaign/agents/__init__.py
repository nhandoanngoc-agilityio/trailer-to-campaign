"""The agents: Watcher -> Copywriter -> Checker, in a fixed order (ADR 0004)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

# An agent's prompts live in prompts/ next to it, so a change to what an agent is told
# shows up as a prompt change (and, from T8, runs the Eval Set).
PROMPTS = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8").strip()


class ChecksFailed(ValueError):
    """An agent's submission failed the code's checks; every problem goes back to the agent at once.

    uses_a_try is False when the submission still got somewhere. details fill in the agent's rejection
    prompt, and logged goes into the Run Log beside the problems.
    """

    def __init__(
        self,
        summary: str,
        problems: list[str],
        *,
        uses_a_try: bool = True,
        details: dict[str, str] | None = None,
        logged: dict[str, Any] | None = None,
    ) -> None:
        self.problems = problems
        self.uses_a_try = uses_a_try
        self.details = details or {}
        self.logged = logged or {}
        super().__init__(f"{summary}:\n" + "\n".join(f"- {p}" for p in problems))
