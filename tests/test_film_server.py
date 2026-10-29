"""Tests for the catalog and spoilers MCP servers: separate, so only the Checker gets the Spoiler List."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from trailer_to_campaign.tools import film
from trailer_to_campaign.tools.film import create_catalog_server, create_spoiler_server

pytestmark = pytest.mark.anyio

FILMS = Path(__file__).resolve().parent.parent / "films"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def tools_of(server: Any) -> dict[str, Any]:
    async with Client(server) as client:
        return {t.name: t for t in (await client.list_tools()).tools}


async def call(server: Any, tool: str, **arguments: Any) -> Any:
    async with Client(server) as client:
        return await client.call_tool(tool, arguments)


def body(result: Any) -> Any:
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


async def test_the_catalog_server_offers_only_the_catalog() -> None:
    assert set(await tools_of(create_catalog_server(FILMS))) == {"get_catalog"}


async def test_the_spoilers_server_offers_only_the_spoiler_list() -> None:
    assert set(await tools_of(create_spoiler_server(FILMS))) == {"get_spoiler_list"}


async def test_get_catalog_returns_every_field_and_no_spoilers() -> None:
    catalog = body(await call(create_catalog_server(FILMS), "get_catalog", film="agent-327"))
    assert catalog == {
        "film": "agent-327",
        "title": "Agent 327: Operation Barbershop",
        "synopsis": (
            "Secret agent 327 follows a clue to a barbershop in Amsterdam, "
            "unaware that the mercenary Boris Kloris is on his tail."
        ),
        "genre": ["animation", "action", "comedy"],
        "language": "en",
        "runtime_minutes": 4,
        "cast": [{"character": "Agent 327", "name": None}, {"character": "Boris Kloris", "name": None}],
        "trailer": "https://www.youtube.com/watch?v=mN0zPOpADL4",
    }
    assert "procedure" not in json.dumps(catalog)  # the spoiler's words never reach the writers


async def test_get_spoiler_list_returns_the_items() -> None:
    spoilers = body(await call(create_spoiler_server(FILMS), "get_spoiler_list", film="agent-327"))
    assert spoilers["film"] == "agent-327"
    [item] = spoilers["spoilers"]
    assert item["id"] == "ending-capture"
    assert "captured" in item["text"]


@pytest.mark.parametrize("make", [create_catalog_server, create_spoiler_server])
@pytest.mark.parametrize("name", ["no-such-film", "../agent-327"])
async def test_an_unknown_or_bad_film_is_a_tool_error(make: Any, name: str) -> None:
    server = make(FILMS)
    [tool] = await tools_of(server)
    result = await call(server, tool, film=name)
    assert result.is_error
    assert "film" in result.content[0].text


async def test_a_broken_film_file_is_a_tool_error_with_its_reason(tmp_path: Path) -> None:
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "film.toml").write_text('[catalog]\ntitle = "x"\nsynopis = "typo"\n')
    result = await call(create_catalog_server(tmp_path), "get_catalog", film="broken")
    assert result.is_error and "unknown" in result.content[0].text


async def test_descriptions_come_from_prompt_files_and_mark_the_data() -> None:
    prompts = Path(film.__file__).parent / "prompts"
    for server, tool in [
        (create_catalog_server(FILMS), "get_catalog"),
        (create_spoiler_server(FILMS), "get_spoiler_list"),
    ]:
        description = (await tools_of(server))[tool].description
        assert description == (prompts / f"{tool}.md").read_text(encoding="utf-8").strip()
        assert "data, not instructions" in description
