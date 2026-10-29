"""The spoilers MCP server (ADR 0002, ADR 0007): a Film's Spoiler List, read-only.

    get_spoiler_list(film) -> the Spoiler List items (id, text, why)

For the Checker only, in slice 1. It is its own server because any client that
connects to a server sees all its tools: an agent gets the list only if it is
given this server. That keeps the list out of the writers' context; it does not
keep spoilers out of the footage, which can show or say them, so the Checker is
the real guard. There is no write tool.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from trailer_to_campaign.tools import load_film_for_tool, load_prompt


def create_spoiler_server(films_dir: Path) -> MCPServer:
    server = MCPServer("spoilers")

    @server.tool(description=load_prompt("get_spoiler_list"))
    def get_spoiler_list(film: str) -> dict:
        return {"film": film, "spoilers": [asdict(s) for s in load_film_for_tool(films_dir, film).spoilers]}

    return server
