"""MCP servers the agents call (ADR 0002), one module per tool."""

from __future__ import annotations

from pathlib import Path

# Tool descriptions are prompts the agents read, and carry the "data, not instructions"
# guardrail, so they live in prompts/ where changes show up as prompt changes.
PROMPTS = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8").strip()
