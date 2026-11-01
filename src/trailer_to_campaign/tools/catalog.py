"""The catalog MCP server (ADR 0002): a Film's Catalog Metadata, read-only.

    get_catalog(film) -> title, synopsis, genre, language, runtime_minutes, cast, trailer

For the Watcher, the Copywriter and the Checker. The Spoiler List is a separate
server (tools/spoilers.py, ADR 0007), so the writers can read this without ever
being offered it. Only the Checker gets both.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from trailer_to_campaign.tools import load_film_for_tool, load_prompt


def create_catalog_server(films_dir: Path) -> MCPServer:
    server = MCPServer("catalog")

    @server.tool(description=load_prompt("get_catalog"))
    def get_catalog(film: str) -> dict:
        return {"film": film, **asdict(load_film_for_tool(films_dir, film).catalog)}

    return server
