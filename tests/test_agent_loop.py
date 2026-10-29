"""Tests for McpTools.using: an agent's own error reaches the caller as itself."""

from __future__ import annotations

from contextlib import AsyncExitStack
from decimal import Decimal
from typing import Any

import pytest

from trailer_to_campaign.agents.loop import McpTools
from trailer_to_campaign.budget import FilmBudgetReached

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def broken_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Closing the MCP clients fails, as a server stream can on the way out."""

    async def connect(cls: Any, stack: AsyncExitStack, servers: Any) -> McpTools:
        async def boom() -> None:
            raise RuntimeError("stream closed")

        stack.push_async_callback(boom)
        return McpTools()

    monkeypatch.setattr(McpTools, "connect", classmethod(connect))


async def test_a_film_budget_stop_survives_a_failed_teardown(broken_teardown: None) -> None:
    async def work(tools: McpTools) -> None:
        raise FilmBudgetReached(Decimal("1.9"), Decimal("2"), Decimal("0.3"))

    with pytest.raises(FilmBudgetReached):
        await McpTools.using([], work)


async def test_a_failed_teardown_after_success_is_still_raised(broken_teardown: None) -> None:
    async def work(tools: McpTools) -> str:
        return "done"

    with pytest.raises(RuntimeError, match="stream closed"):
        await McpTools.using([], work)
