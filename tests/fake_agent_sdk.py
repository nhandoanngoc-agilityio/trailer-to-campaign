"""A scripted stand-in for the Agent SDK's query(), shared by the SDK Watcher tests.

It plays the part of the Claude Code CLI: each scripted turn is one model response
that asks for tool calls. For every call it runs the PreToolUse hooks, calls the tool
through the SDK's real in-process MCP server (so the SDK's own input checks and
content conversion run), then the PostToolUse hooks, and stops when a hook says so.
It streams message_start and message_delta events with usage, and ends with a
ResultMessage. Nothing starts the CLI, reaches the network or costs money.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    CLIConnectionError,
    ResultError,
    ResultMessage,
    StreamEvent,
    SystemMessage,
)
from mcp import Client

MODEL = "claude-opus-5-5"
INPUT, OUTPUT = 2_000, 300  # each scripted response's usage


@dataclass
class Call:
    name: str  # as the model names it: mcp__watcher__watch_footage, or a built-in such as Bash
    arguments: dict[str, Any]


Turn = Callable[["Session"], list[Call]]


@dataclass
class Seen:
    """One tool call as the fake CLI saw it."""

    name: str
    arguments: dict[str, Any]
    denied: str | None = None  # the PreToolUse hook's reason, if it denied the call
    content: list[dict[str, Any]] = field(default_factory=list)  # the tool result, in MCP form
    is_error: bool = False

    def json(self) -> Any:
        return json.loads(self.content[0]["text"])


@dataclass
class Session:
    seen: list[Seen] = field(default_factory=list)

    def result_of(self, tool: str) -> Seen:
        return next(s for s in reversed(self.seen) if s.name.endswith(f"__{tool}") and not s.is_error)


class FakeQuery:
    """Use as the query= argument: FakeQuery(turns)(prompt=..., options=...)."""

    def __init__(
        self,
        turns: list[Turn],
        *,
        end: str = "success",  # the ResultMessage subtype when the turns run out
        extra_usage: dict[str, dict[str, Any]] | None = None,  # model_usage the stream never showed
        crash_after: int | None = None,  # stop after this many turns with no ResultMessage
        open_last: bool = False,  # the last response starts but never finishes (no message_delta)
        prompts: list[int] | None = None,  # each response's prompt tokens (default INPUT)
        model: str = MODEL,  # the model id the stream reports
        never_starts: bool = False,  # the CLI fails before sending anything (not installed, say)
        stalls: bool = False,  # the last response starts, then nothing more ever comes (T17)
    ) -> None:
        self.turns = list(turns)
        self.model, self.never_starts, self.stalls = model, never_starts, stalls
        self.end, self.extra_usage, self.crash_after, self.open_last = end, extra_usage or {}, crash_after, open_last
        self.prompts = prompts or []
        self.input_total = 0
        self.session = Session()
        self.options: ClaudeAgentOptions | None = None
        self.prompt: str | None = None
        self.responses = 0

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        self.prompt, self.options = prompt, options
        return self._run(options)

    async def _run(self, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        if self.never_starts:
            raise CLIConnectionError("Claude Code not found")
        yield SystemMessage(subtype="init", data={"type": "system", "subtype": "init"})
        server = options.mcp_servers["watcher"]["instance"]  # type: ignore[index]
        async with Client(server) as client:
            for turn in self.turns:
                if self.crash_after is not None and self.responses >= self.crash_after:
                    return  # the CLI died: no result
                self.responses += 1
                yield self._event({"type": "message_start", "message": self._message()})
                if self.stalls and self.responses == len(self.turns):
                    import anyio

                    await anyio.sleep_forever()
                if self.open_last and self.responses == len(self.turns):
                    if self.end != "error_during_execution":
                        return  # the CLI died mid-response: no result
                    break  # the CLI reports the crash, with the partial response's usage
                yield self._event({"type": "message_delta", "usage": {"output_tokens": OUTPUT}})
                stop = False
                for call in turn(self.session):
                    seen = Seen(call.name, call.arguments)
                    self.session.seen.append(seen)
                    seen.denied = await self._pre(options, call)
                    if seen.denied:
                        seen.is_error = True
                        continue
                    result = await client.call_tool(call.name.removeprefix("mcp__watcher__"), call.arguments)
                    seen.content = [c.model_dump(exclude_none=True, by_alias=True) for c in result.content]
                    seen.is_error = bool(result.is_error)
                    stop = stop or await self._post(options, call, seen)
                if stop:
                    break
        yield self._result()
        if self.end != "success":  # like the real query(): an error result, then ResultError
            raise ResultError(f"run ended with {self.end}", data={"subtype": self.end})

    def _message(self) -> dict[str, Any]:
        n = self.responses - 1
        tokens = self.prompts[n] if n < len(self.prompts) else INPUT
        self.input_total += tokens
        return {
            "id": f"msg_{self.responses}",
            "model": self.model,
            "usage": {"input_tokens": tokens, "output_tokens": 1, "cache_read_input_tokens": 0},
        }

    def _event(self, event: dict[str, Any]) -> StreamEvent:
        return StreamEvent(uuid=f"u{self.responses}", session_id="s1", event=event)

    async def _pre(self, options: ClaudeAgentOptions, call: Call) -> str | None:
        for matcher in (options.hooks or {}).get("PreToolUse", []):
            for hook in matcher.hooks:
                out = await hook(_hook_input("PreToolUse", call), "tu_1", {"signal": None})
                specific = out.get("hookSpecificOutput") or {}
                if specific.get("permissionDecision") == "deny":
                    return specific.get("permissionDecisionReason", "denied")
        if call.name not in options.allowed_tools:
            return "not allowed"  # dontAsk: anything not pre-approved is refused
        return None

    async def _post(self, options: ClaudeAgentOptions, call: Call, seen: Seen) -> bool:
        """PostToolUse after a tool that worked; PostToolUseFailure after an error result, as the CLI may."""
        event = "PostToolUseFailure" if seen.is_error else "PostToolUse"
        extra = {"error": json.dumps(seen.content)} if seen.is_error else {"tool_response": seen.content}
        stop = False
        for matcher in (options.hooks or {}).get(event, []):
            for hook in matcher.hooks:
                out = await hook({**_hook_input(event, call), **extra}, "tu_1", {})
                stop = stop or out.get("continue_") is False
        return stop

    def _result(self) -> ResultMessage:
        finished = self.responses - (1 if self.open_last else 0)  # a cut-off response reports no output
        usage = {
            self.model: {
                "inputTokens": self.input_total,
                "outputTokens": OUTPUT * finished,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "webSearchRequests": 0,
                "costUSD": 0.0,
                "contextWindow": 1_000_000,
                "maxOutputTokens": 16_000,
            },
            **self.extra_usage,
        }
        return ResultMessage(
            subtype=self.end,
            duration_ms=1,
            duration_api_ms=1,
            is_error=self.end != "success",
            num_turns=self.responses,
            session_id="s1",
            model_usage=usage,  # type: ignore[arg-type]
        )


def _hook_input(event: str, call: Call) -> dict[str, Any]:
    return {
        "hook_event_name": event,
        "session_id": "s1",
        "transcript_path": "",
        "cwd": "",
        "tool_name": call.name,
        "tool_input": call.arguments,
        "tool_use_id": "tu_1",
    }


def tool(name: str, **arguments: Any) -> Call:
    return Call(f"mcp__watcher__{name}", arguments)
