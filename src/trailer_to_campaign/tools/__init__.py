"""MCP servers the agents call (ADR 0002), one module per tool."""

from __future__ import annotations

from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from trailer_to_campaign.catalog import Film, load_film

# Tool descriptions are prompts the agents read, and carry the "data, not instructions"
# guardrail, so they live in prompts/ where changes show up as prompt changes.
PROMPTS = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8").strip()


def load_film_for_tool(films_dir: Path, film: str) -> Film:
    """Load a Film for a tool call; a bad Film or file becomes a tool error with its reason."""
    try:
        return load_film(films_dir, film)
    except ValueError as err:  # FilmFileError, or a bad slug; neither message holds a local path
        raise ToolError(str(err)) from err
