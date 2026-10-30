"""The Watcher on the Claude Agent SDK (T11): v0's tools, prompts and gate, on Claude Code's harness.

    moments = await watch_trailer_sdk(film, source, servers=watcher_servers(settings), meter=meter, api_key=key)

The SDK runs the agent loop; everything that makes the Watcher safe stays ours:
- every tool call goes through watcher.tool_gate, the same gate v0 uses (the Run is
  pinned to its Film and trailer, and each submission is grounded);
- the harness is locked down: no built-in tools (no Bash, files or web), no settings,
  CLAUDE.md or hooks from disk, only the Watcher's four tools allowed, and a hook that
  refuses any other tool by name;
- the Run stops right after a submission is accepted or the gate gives up, through a
  PostToolUse hook, so the SDK still reports what it spent.

The SDK can't be metered call by call (ADR 0009). Before it starts, its own spending
cap is set to what's left of the Film Budget less one worst-case response, so it stops
while the Run is still inside the Film Budget; a prompt past PROMPT_GUARD stops the Run, so
no response can outgrow that margin. Each response is charged at our prices
from the usage it streams; usage the stream never showed is charged from the result;
a response cut off, or a request in flight when the CLI dies, is charged its worst case.
"""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKError,
    HookMatcher,
    ResultError,
    ResultMessage,
    SdkMcpTool,
    StreamEvent,
    ToolAnnotations,
    create_sdk_mcp_server,
)
from claude_agent_sdk import query as sdk_query
from claude_agent_sdk import tool as sdk_tool

from trailer_to_campaign.agents import load_prompt
from trailer_to_campaign.agents.key_moments import KeyMoment
from trailer_to_campaign.agents.loop import MAX_TOKENS, AgentFailed, McpTools, ToolResult
from trailer_to_campaign.agents.watcher import EFFORT, MAX_TURNS, MODEL, start_watching, submit_tool, tool_gate
from trailer_to_campaign.budget import FilmBudgetReached, FilmMeter
from trailer_to_campaign.pricing import PRICES, Usage, worst_case_usd

SERVER = "watcher"
AGENT = "watcher"
# The largest prompt the cap's margin allows for: one response at this size and MAX_TOKENS
# can't take the Run past its Film Budget. The largest Watcher prompt seen (v0, the T8 Eval
# Set) was about 26,000 tokens.
MAX_CONTEXT_TOKENS = 100_000
# Once a response's prompt passes this, the Run stops before another request: the next
# prompt adds at most one reply and one turn's tool results, so it stays inside the margin.
PROMPT_GUARD = 50_000
# Claude Code keeps a tool result inline up to a size, and past it saves the result to a
# file the Watcher can't read (it has no file tools); the footage timeline must stay inline.
RESULT_CHARS = 1_000_000
# The CLI's own retries are off: a retried call could be billed twice and charged once.
CLI_ENV = {
    "CLAUDE_CODE_MAX_RETRIES": "0",
    "CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES": "0",
    "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(MAX_TOKENS),
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",  # no background model call for a session title
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}

Query = Callable[..., AsyncIterator[Any]]


def tool_name(name: str) -> str:
    return f"mcp__{SERVER}__{name}"


@dataclass
class _Session:
    """What the SDK session has done that the meter and the hooks need to know."""

    meter: FilmMeter
    failure: BaseException | None = None  # the gate gave up, or the Film Budget was reached, inside a tool
    finished: bool = False  # a hook ended the session: no request follows the last tool
    in_flight: bool = True  # a request may be on its way (at the start, and after each tool result)
    open: tuple[str, Usage] | None = None  # the response streaming now: its model and prompt usage
    seen: dict[str, Usage] = field(default_factory=dict)  # usage charged so far, by model
    last_prompt: int = 0  # the latest response's prompt tokens

    def stream(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            self.open = (message.get("model") or MODEL, _usage(message.get("usage") or {}))
            self.last_prompt = self.open[1].prompt_tokens
            self.in_flight = False
        elif kind == "message_delta" and self.open is not None:
            model, prompt = self.open
            output = int((event.get("usage") or {}).get("output_tokens") or 0)
            self.open = None
            self._charge(model, Usage(prompt.input, output, prompt.cache_read, prompt.cache_write_5m,
                                      prompt.cache_write_1h))  # fmt: skip

    def result(self, model_usage: dict[str, Any]) -> None:
        """Charge what the result reports beyond what the stream showed (background calls, say)."""
        for key, used in model_usage.items():
            model = used.get("canonicalModel") or key
            total = Usage(
                input=int(used.get("inputTokens") or 0),
                output=int(used.get("outputTokens") or 0),
                cache_read=int(used.get("cacheReadInputTokens") or 0),
                cache_write_1h=int(used.get("cacheCreationInputTokens") or 0),  # no 5m/1h split: the dearer
            )
            left = _less(total, self.seen.get(model, Usage()))
            if left == Usage():
                continue
            if model in PRICES:
                self._charge(model, left)
            else:
                self.meter.charge_after(
                    model, agent=AGENT, cost=Decimal(str(used.get("costUSD") or 0)), charged="sdk_estimate"
                )

    def unfinished(self) -> None:
        """Fail closed: a response cut off, or a request that may have been sent, is charged its worst case."""
        if self.open is not None:
            model, prompt = self.open
            self.open = None
            worst = worst_case_usd(model, prompt.prompt_tokens, MAX_TOKENS)
            self.meter.charge_after(model, agent=AGENT, cost=worst, charged="worst_case")
        elif self.in_flight and not self.finished:
            worst = worst_case_usd(MODEL, MAX_CONTEXT_TOKENS, MAX_TOKENS)
            self.meter.charge_after(MODEL, agent=AGENT, cost=worst, charged="worst_case")
        self.in_flight = False

    def _charge(self, model: str, usage: Usage) -> None:
        self.seen[model] = _plus(self.seen.get(model, Usage()), usage)
        self.meter.charge_after(model, agent=AGENT, usage=usage)


def _usage(raw: dict[str, Any]) -> Usage:
    split = raw.get("cache_creation") or {}
    if split:
        write_5m, write_1h = split.get("ephemeral_5m_input_tokens") or 0, split.get("ephemeral_1h_input_tokens") or 0
    else:
        write_5m, write_1h = 0, raw.get("cache_creation_input_tokens") or 0
    return Usage(
        input=raw.get("input_tokens") or 0,
        output=raw.get("output_tokens") or 0,
        cache_read=raw.get("cache_read_input_tokens") or 0,
        cache_write_5m=write_5m,
        cache_write_1h=write_1h,
    )


def _plus(a: Usage, b: Usage) -> Usage:
    return Usage(a.input + b.input, a.output + b.output, a.cache_read + b.cache_read,
                 a.cache_write_5m + b.cache_write_5m, a.cache_write_1h + b.cache_write_1h)  # fmt: skip


def _less(total: Usage, seen: Usage) -> Usage:
    writes = max(0, total.cache_write_1h - seen.cache_write_5m - seen.cache_write_1h)
    return Usage(max(0, total.input - seen.input), max(0, total.output - seen.output),
                 max(0, total.cache_read - seen.cache_read), 0, writes)  # fmt: skip


def _to_sdk(result: ToolResult) -> dict[str, Any]:
    content = []
    for block in result.content:
        if block["type"] == "image":
            content.append(
                {"type": "image", "data": block["source"]["data"], "mimeType": block["source"]["media_type"]}
            )
        else:
            content.append({"type": "text", "text": block.get("text", "")})
    return {"content": content, "is_error": result.is_error}


def sdk_tools(definitions: list[dict[str, Any]], call_tool: Any, session: _Session) -> list[SdkMcpTool[Any]]:
    """The Watcher's tools for the SDK, each one a thin wrapper around the shared gate."""

    def wrap(name: str, description: str, schema: dict[str, Any]) -> SdkMcpTool[Any]:
        @sdk_tool(name, description, schema, annotations=ToolAnnotations(maxResultSizeChars=RESULT_CHARS))
        async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
            try:
                result = await call_tool(name, arguments)
            except (AgentFailed, FilmBudgetReached) as err:
                # The SDK would only show an exception to the model; the Run must stop instead.
                session.failure = err
                result = ToolResult.error(f"Stopping: {err}")
            if session.last_prompt > PROMPT_GUARD and session.failure is None:
                session.failure = AgentFailed(
                    f"the {AGENT}'s prompt grew past {PROMPT_GUARD} tokens ({session.last_prompt}); "
                    "stopped so the Film Budget's margin holds"
                )
            session.meter.event("tool_call", agent=AGENT, tool=name, is_error=result.is_error)
            session.in_flight = True
            return _to_sdk(result)

        return handler

    return [wrap(d["name"], d["description"], d["input_schema"]) for d in definitions]


def sdk_hooks(allowed: list[str], session: _Session, done: Callable[[], bool]) -> dict[str, list[HookMatcher]]:
    async def only_the_watchers_tools(hook: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
        if hook.get("tool_name") in allowed:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "The Watcher may use only its own tools.",
            }
        }

    async def stop_when_decided(hook: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
        if session.failure is None and not done():
            return {}
        session.finished, session.in_flight = True, False
        return {"continue_": False, "stopReason": "the Watcher is done"}

    return {
        "PreToolUse": [HookMatcher(hooks=[only_the_watchers_tools])],
        "PostToolUse": [HookMatcher(hooks=[stop_when_decided])],
    }


def sdk_options(
    *, server: Any, allowed: list[str], hooks: dict[str, list[HookMatcher]], cap: Decimal, api_key: str, cwd: str
) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=load_prompt("watcher"),
        tools=[],
        mcp_servers={SERVER: server},
        strict_mcp_config=True,
        allowed_tools=allowed,
        permission_mode="dontAsk",
        setting_sources=[],
        hooks=hooks,  # type: ignore[arg-type]
        model=MODEL,
        effort=EFFORT,  # type: ignore[arg-type]
        max_turns=MAX_TURNS,
        max_budget_usd=float(cap),
        include_partial_messages=True,
        cwd=cwd,
        env={"ANTHROPIC_API_KEY": api_key, **CLI_ENV},
    )


async def watch_trailer_sdk(
    film: str, source: str, *, servers: list[Any], meter: FilmMeter, api_key: str, query: Query = sdk_query
) -> list[KeyMoment]:
    margin = worst_case_usd(MODEL, MAX_CONTEXT_TOKENS, MAX_TOKENS)
    cap = meter.remaining() - margin
    if cap <= 0:
        meter.event("film_budget_reached", model=MODEL, spent_usd=meter.spent, limit_usd=meter.limit,
                    next_call_usd=margin)  # fmt: skip
        raise FilmBudgetReached(meter.spent, meter.limit, margin)
    watching = start_watching(film, source, meter)
    session = _Session(meter)

    async def watch(mcp: McpTools) -> list[KeyMoment]:
        definitions = sorted([*mcp.definitions, submit_tool()], key=lambda t: t["name"])
        allowed = [tool_name(d["name"]) for d in definitions]
        server = create_sdk_mcp_server(SERVER, tools=sdk_tools(definitions, tool_gate(watching, mcp), session))
        hooks = sdk_hooks(allowed, session, lambda: watching.gate.accepted is not None)
        task = load_prompt("watcher_task").format(film=film, source=source)
        result: ResultMessage | None = None
        error: ClaudeSDKError | None = None
        with tempfile.TemporaryDirectory(prefix="t2c-watcher-") as cwd:
            options = sdk_options(server=server, allowed=allowed, hooks=hooks, cap=cap, api_key=api_key, cwd=cwd)
            try:
                async for message in query(prompt=task, options=options):
                    if isinstance(message, StreamEvent) and message.parent_tool_use_id is None:
                        session.stream(message.event)
                    elif isinstance(message, ResultMessage):
                        result = message
            except ClaudeSDKError as err:
                error = err
            finally:
                if result is not None and result.model_usage:
                    session.result(dict(result.model_usage))
                if result is None or result.subtype == "error_during_execution":
                    session.unfinished()
        return _outcome(session, watching.gate.accepted, result, error, meter, margin)

    return await McpTools.using(servers, watch)


def _outcome(
    session: _Session,
    accepted: list[KeyMoment] | None,
    result: ResultMessage | None,
    error: ClaudeSDKError | None,
    meter: FilmMeter,
    margin: Decimal,
) -> list[KeyMoment]:
    if session.failure is not None:
        raise session.failure
    if accepted is not None:
        return accepted
    subtype = result.subtype if result is not None else getattr(error, "subtype", None)
    if subtype == "error_max_budget_usd":
        meter.event("film_budget_reached", model=MODEL, spent_usd=meter.spent, limit_usd=meter.limit,
                    next_call_usd=margin)  # fmt: skip
        raise FilmBudgetReached(meter.spent, meter.limit, margin)
    if subtype == "error_max_turns":
        raise AgentFailed(f"the {AGENT} did not finish in {MAX_TURNS} turns")
    why = subtype or (type(error).__name__ if error else "no result")
    raise AgentFailed(f"the {AGENT} stopped without Key Moments ({why})") from (
        error if not isinstance(error, ResultError) else None
    )
