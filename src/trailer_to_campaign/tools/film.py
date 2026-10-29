"""The Film MCP servers (ADR 0002): Catalog Metadata and the Spoiler List, read-only.

They are two servers, not one, so the Spoiler List reaches only the agents given
the `spoilers` server (the Checker, in slice 1). The Watcher and Copywriter get
`catalog` alone, so spoiler text never enters the context of an agent that
writes posts. Neither server has a write tool.

    catalog:  get_catalog(film)      -> title, synopsis, genre, language, runtime_minutes, cast, trailer
    spoilers: get_spoiler_list(film) -> the Spoiler List items
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from trailer_to_campaign.catalog import load_film
from trailer_to_campaign.tools import load_prompt


def create_catalog_server(films_dir: Path) -> MCPServer:
    server = MCPServer("catalog")

    @server.tool(description=load_prompt("get_catalog"))
    def get_catalog(film: str) -> dict:
        try:
            return {"film": film, **asdict(load_film(films_dir, film).catalog)}
        except (ValueError, OSError) as err:
            raise ToolError(str(err)) from err

    return server


def create_spoiler_server(films_dir: Path) -> MCPServer:
    server = MCPServer("spoilers")

    @server.tool(description=load_prompt("get_spoiler_list"))
    def get_spoiler_list(film: str) -> dict:
        try:
            return {"film": film, "spoilers": [asdict(s) for s in load_film(films_dir, film).spoilers]}
        except (ValueError, OSError) as err:
            raise ToolError(str(err)) from err

    return server
