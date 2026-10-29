"""The hand-written agent loop on the Claude API (no Agent SDK until T11), metered by the Film Budget.

Each turn:
1. count the whole prompt's tokens (count_tokens is free and exact),
2. send it inside meter.call(), which reserves the worst case first and charges the actual usage,
3. append the reply unchanged (thinking blocks must be passed back as they came),
4. run every tool the reply asks for and send all the results back in one message.

The loop ends when the agent says it is done (is_done), and fails on a reply that was
cut off or refused, or after max_turns. The client must be built with max_retries=0:
an SDK retry of a timed-out call could be billed twice and metered once.

MCP tools are reached through in-process MCP clients (ADR 0002), so an agent sees only
the tools of the servers it is given (ADR 0007).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, TypeVar

from mcp import Client
from mcp.types import ImageContent, TextContent

from trailer_to_campaign.agents import ChecksFailed
from trailer_to_campaign.budget import FilmMeter
from trailer_to_campaign.pricing import Usage

MAX_TOKENS = 16_000

log = logging.getLogger(__name__)

T = TypeVar("T")


class AgentFailed(RuntimeError):
    """The agent stopped without a usable result; the Run ends in an error."""


@dataclass
class ToolResult:
    content: list[dict[str, Any]]  # Claude content blocks
    is_error: bool = False
    data: Any = field(default=None, repr=False)  # the MCP result, for the agent's own bookkeeping

    @classmethod
    def text(cls, text: str, is_error: bool = False) -> ToolResult:
        return cls([_text(text)], is_error=is_error)

    @classmethod
    def error(cls, message: str) -> ToolResult:
        return cls.text(message, is_error=True)


@dataclass
class Submissions:
    """The gate between an agent and the next stage of the Run.

    Each submission is checked in code (check raises ChecksFailed). A failed one is
    logged and sent back with every problem, at most `limit` times; then the agent
    has failed. Only an accepted submission moves on.
    """

    meter: FilmMeter
    agent: str  # "watcher"
    noun: str  # what it submits: "Key Moments"
    failure: str  # how a failed check reads: "were not grounded"
    rejected_prompt: str  # what the agent is told, with {left} and {problems}
    limit: int
    count: int = 0
    accepted: list[Any] | None = None

    def submit(self, check: Callable[[], list[Any]]) -> ToolResult:
        self.count += 1
        try:
            result = check()
        except ChecksFailed as err:
            event = f"{self.noun.lower().replace(' ', '_')}_rejected"
            self.meter.event(event, agent=self.agent, attempt=self.count, problems=err.problems)
            if self.count >= self.limit:
                raise AgentFailed(
                    f"the {self.agent.capitalize()}'s {self.noun} {self.failure} after {self.limit} tries"
                ) from err
            problems = "\n".join(f"- {p}" for p in err.problems)
            return ToolResult.error(self.rejected_prompt.format(left=self.limit - self.count, problems=problems))
        self.accepted = result
        return ToolResult.text(f"Accepted {len(result)} {self.noun}.")


class McpTools:
    """The tools of a set of MCP servers, with their definitions in Claude's tool format."""

    def __init__(self) -> None:
        self.definitions: list[dict[str, Any]] = []
        self._clients: dict[str, Client] = {}

    @classmethod
    async def using(cls, servers: Sequence[Any], work: Callable[[McpTools], Awaitable[T]]) -> T:
        """Run work with the servers' tools connected.

        An error from work (FilmBudgetReached, AgentFailed) is raised as itself once the
        clients are closed, not wrapped in the ExceptionGroup of the MCP clients' task group,
        so callers can catch it by type. If closing the clients also fails, that error is
        logged and the work's error still wins: it is what decides how the Run ends.
        """
        failure: Exception | None = None
        try:
            async with AsyncExitStack() as stack:
                tools = await cls.connect(stack, servers)
                try:
                    return await work(tools)
                except Exception as err:
                    failure = err
        except Exception:
            if failure is None:
                raise
            log.warning("closing the MCP clients failed after %r", failure, exc_info=True)
        assert failure is not None
        raise failure

    @classmethod
    async def connect(cls, stack: AsyncExitStack, servers: Sequence[Any]) -> McpTools:
        tools = cls()
        for server in servers:
            client = await stack.enter_async_context(Client(server))
            for tool in (await client.list_tools()).tools:
                if tool.name in tools._clients:
                    raise ValueError(f"two servers offer a tool named {tool.name!r}")
                tools._clients[tool.name] = client
                tools.definitions.append(
                    {"name": tool.name, "description": tool.description or "", "input_schema": tool.input_schema}
                )
        return tools

    def __contains__(self, name: str) -> bool:
        return name in self._clients

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        result = await self._clients[name].call_tool(name, arguments)
        return ToolResult([_block(c) for c in result.content] or [_text("(no content)")], result.is_error, result)


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _block(content: Any) -> dict[str, Any]:
    if isinstance(content, TextContent):
        return _text(content.text)
    if isinstance(content, ImageContent):
        return {"type": "image", "source": {"type": "base64", "media_type": content.mime_type, "data": content.data}}
    return _text(f"({content.type} content is not supported and was left out)")


@dataclass
class AgentLoop:
    client: Any  # anthropic.AsyncAnthropic, built with max_retries=0
    meter: FilmMeter
    agent: str
    model: str
    effort: str
    system: str
    tools: list[dict[str, Any]]
    max_turns: int
    nudge: str  # sent when the agent stops talking without finishing

    async def run(
        self,
        messages: list[Any],
        call_tool: Callable[[str, dict[str, Any]], Awaitable[ToolResult]],
        is_done: Callable[[], bool],
    ) -> None:
        tools = sorted(self.tools, key=lambda t: t["name"])  # a stable order keeps the cached prefix valid
        for _ in range(self.max_turns):
            params: dict[str, Any] = {
                "model": self.model,
                "max_tokens": MAX_TOKENS,
                "system": self.system,
                "tools": tools,
                "messages": messages,
                "output_config": {"effort": self.effort},
                "cache_control": {"type": "ephemeral"},  # the history is re-sent every turn
            }
            counted = await self.client.messages.count_tokens(**{k: v for k, v in params.items() if k != "max_tokens"})
            with self.meter.call(self.model, counted.input_tokens, MAX_TOKENS, agent=self.agent) as call:
                response = await self.client.messages.create(**params)
                call.record(Usage.from_api(response.usage))
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "tool_use":
                results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    result = await call_tool(block.name, block.input)
                    self.meter.event("tool_call", agent=self.agent, tool=block.name, is_error=result.is_error)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result.content,
                            "is_error": result.is_error,
                        }
                    )
                messages.append({"role": "user", "content": results})
                if is_done():
                    return
            elif response.stop_reason == "end_turn":
                messages.append({"role": "user", "content": self.nudge})
            elif response.stop_reason != "pause_turn":
                raise AgentFailed(f"the {self.agent} stopped with {response.stop_reason!r}")
        raise AgentFailed(f"the {self.agent} did not finish in {self.max_turns} turns")
