"""Tests for the agent loop: McpTools.using keeps an agent's own error, and every model call has a deadline."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trailer_to_campaign.agents import loop
from trailer_to_campaign.agents.loop import AgentFailed, AgentLoop, McpTools
from trailer_to_campaign.budget import FilmBudgetReached, FilmMeter
from trailer_to_campaign.run_log import RunLog

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


def test_a_rejection_logs_the_submission_only_when_given_it(tmp_path: Path) -> None:
    from trailer_to_campaign.agents import ChecksFailed
    from trailer_to_campaign.agents.loop import Submissions

    meter = FilmMeter(Decimal("2"), RunLog.create(tmp_path / "runs", "sample"))
    gate = Submissions(meter, "copywriter", "Social Posts", "were not traced", "{problems} {left}", 3)

    def fails() -> list[Any]:
        raise ChecksFailed("Social Posts not traced", ["bad"])

    gate.submit(fails)
    gate.submit(fails, submitted=[{"post": 1}])
    first, second = logged(meter.run_log.path, "social_posts_rejected")
    assert "submitted" not in first
    assert second["submitted"] == [{"post": 1}]


def test_a_rejection_can_say_it_uses_no_try_and_fill_in_the_prompt(tmp_path: Path) -> None:
    from trailer_to_campaign.agents import ChecksFailed
    from trailer_to_campaign.agents.loop import AgentFailed, Submissions

    meter = FilmMeter(Decimal("2"), RunLog.create(tmp_path / "runs", "sample"))
    gate = Submissions(meter, "copywriter", "Social Posts", "were not traced", "{left} {problems} {what}", 2)

    def progress() -> list[Any]:
        raise ChecksFailed("no", ["bad"], uses_a_try=False, details={"what": "x"}, logged={"kept": ["y"]})

    def nothing() -> list[Any]:
        raise ChecksFailed("no", ["bad"], details={"what": "x"})

    assert gate.submit(progress).content[0]["text"] == "2 - bad x"  # no try used
    assert gate.submit(nothing).content[0]["text"] == "1 - bad x"
    with pytest.raises(AgentFailed):
        gate.submit(nothing)
    events = logged(meter.run_log.path, "social_posts_rejected")
    assert [e["attempt"] for e in events] == [1, 2, 3]  # every submission is numbered, counted or not
    assert events[0]["kept"] == ["y"] and "kept" not in events[1]


# T17: a model call that never answers ends the agent, and the meter keeps what it may have cost.


def hung_loop(tmp_path: Path, on: str) -> tuple[AgentLoop, Path]:
    from fake_claude import HangingClient

    meter = FilmMeter(Decimal("2"), RunLog.create(tmp_path / "runs", "sample"))
    agent = AgentLoop(HangingClient(on), meter, "copywriter", "claude-opus-5-5", "medium", "", [], 3, "Go on.")
    return agent, meter.run_log.path


async def run_hung(agent: AgentLoop) -> None:
    await agent.run([{"role": "user", "content": "Write."}], call_tool=None, is_done=lambda: False)  # type: ignore[arg-type]


def logged(path: Path, kind: str) -> list[dict[str, Any]]:
    return [e for e in map(json.loads, path.read_text().splitlines()) if e["event"] == kind]


async def test_a_call_that_never_answers_fails_the_agent_and_is_charged_its_worst_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(loop, "MODEL_CALL_TIMEOUT_S", 0.05)
    agent, log = hung_loop(tmp_path, "create")
    with pytest.raises(AgentFailed, match=r"copywriter.*did not answer within 0\.05 s"):
        await run_hung(agent)
    [call] = logged(log, "model_call")
    assert call["error"] == "TimeoutError" and call["charged"] == "worst_case"  # it may have been billed
    assert agent.meter.spent == Decimal(call["cost_usd"]) == Decimal(call["reserved_usd"]) > 0


async def test_the_clients_own_timeout_also_fails_the_agent_cleanly(tmp_path: Path) -> None:
    # The client's per-read timeout can fire before the loop's deadline: the same clean failure, not a crash.
    import anthropic

    agent, log = hung_loop(tmp_path, "create")

    async def times_out(**params: Any) -> Any:
        raise anthropic.APITimeoutError(request=None)  # type: ignore[arg-type]

    agent.client.messages.create = times_out
    with pytest.raises(AgentFailed, match="copywriter.*timed out"):
        await run_hung(agent)
    [call] = logged(log, "model_call")
    assert call["error"] == "APITimeoutError" and call["charged"] == "worst_case"


async def test_a_token_count_that_never_answers_fails_the_agent_and_costs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(loop, "MODEL_CALL_TIMEOUT_S", 0.05)
    agent, log = hung_loop(tmp_path, "count_tokens")
    with pytest.raises(AgentFailed, match=r"did not answer within 0\.05 s"):
        await run_hung(agent)
    assert logged(log, "model_call") == [] and agent.meter.spent == 0  # count_tokens is free
