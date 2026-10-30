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
while the Run is still inside the Film Budget. Before each tool result goes back, the Run
stops if the next prompt could outgrow that margin, or if what's left in our own prices is
less than the margin (the SDK's cap counts in its own price table). Each response is charged at our prices
from the usage it streams; usage the stream never showed is charged from the result;
a response cut off, or a request in flight when the CLI dies, is charged its worst case.
"""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
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
from trailer_to_campaign.agents.loop import MAX_TOKENS, MODEL_CALL_TIMEOUT_S, AgentFailed, McpTools, ToolResult
from trailer_to_campaign.agents.watcher import EFFORT, MAX_TURNS, MODEL, start_watching, submit_tool, tool_gate
from trailer_to_campaign.budget import FilmBudgetReached, FilmMeter
from trailer_to_campaign.footage.settings import LONG_EDGE_PX
from trailer_to_campaign.pricing import PRICES, Usage, cost_usd, worst_case_usd

SERVER = "watcher"
AGENT = "watcher"
# The largest prompt the cap's margin allows for: one response at this size and MAX_TOKENS
# can't take the Run past its Film Budget. No request is sent that could be larger: the
# next prompt is estimated from this one, a full reply and this turn's tool results. The
# largest Watcher prompt seen (v0, the T8 Eval Set) was about 26,000 tokens.
MAX_CONTEXT_TOKENS = 100_000
# The most image tokens one frame can cost: frames are at most LONG_EDGE_PX on a side,
# and an image costs about (width x height) / 750 tokens.
IMAGE_TOKENS = LONG_EDGE_PX * LONG_EDGE_PX // 750 + 1
# Text tokens are estimated high: at most one per two characters (Vietnamese runs denser than English).
CHARS_PER_TOKEN = 2
# A model id with no price of ours is charged at the dearest one: never guessed low.
DEAREST = max(PRICES, key=lambda m: PRICES[m].output)
# Claude Code keeps a tool result inline up to a size, and past it saves the result to a
# file the Watcher can't read (it has no file tools); the footage timeline must stay inline.
RESULT_CHARS = 1_000_000
# The CLI's own retries are off: a retried call could be billed twice and charged once.
CLI_ENV = {
    "CLAUDE_CODE_MAX_RETRIES": "0",
    "CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES": "0",
    "API_TIMEOUT_MS": str(MODEL_CALL_TIMEOUT_S * 1000),  # the same deadline as v0's calls (T17)
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
    margin: Decimal  # one worst-case response, held back from the SDK's cap
    done: Callable[[], bool]  # the gate has accepted Key Moments
    failure: BaseException | None = None  # the gate gave up, or a limit was reached, inside a tool
    finished: bool = False  # a hook ended the session: no request follows the last tool
    in_flight: bool = False  # a request may be on its way (once the CLI is up, and after each tool result)
    started: bool = False  # the CLI has said anything at all
    open: tuple[str, Usage] | None = None  # the response streaming now: its model and prompt usage
    seen: dict[str, Usage] = field(default_factory=dict)  # usage charged so far, by model id
    last_prompt: int = 0  # the latest response's prompt tokens
    turn_tokens: int = 0  # an estimate of the tool results returned since that response

    def start(self) -> None:
        """The CLI is up, so from now on a request may be sent before we see it."""
        if not self.started:
            self.started, self.in_flight = True, True

    def stream(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            self.open = (message.get("model") or MODEL, Usage.from_api(message.get("usage") or {}))
            self.last_prompt, self.turn_tokens, self.in_flight = self.open[1].prompt_tokens, 0, False
        elif kind == "message_delta" and self.open is not None:
            model, prompt = self.open
            self.open = None
            self._charge(model, replace(prompt, output=int((event.get("usage") or {}).get("output_tokens") or 0)))

    def after_tool(self, result: ToolResult) -> None:
        """Before a tool result goes back (and so before the next request): stop if that request could break a limit.

        Never once the Key Moments are accepted: that work is done and paid for."""
        self.turn_tokens += _tokens(result)
        self.in_flight = True
        if self.failure is not None or self.done():
            return
        next_prompt = self.last_prompt + MAX_TOKENS + self.turn_tokens
        if next_prompt > MAX_CONTEXT_TOKENS:
            self.failure = AgentFailed(
                f"the {AGENT}'s next prompt could reach {next_prompt} tokens, past the {MAX_CONTEXT_TOKENS} "
                "the Film Budget's margin covers"
            )
        elif self.meter.remaining() - self._open_worst() < self.margin:
            # In our prices: the SDK's own cap counts in its bundled price table.
            self.meter.event("film_budget_reached", model=MODEL, spent_usd=self.meter.spent,
                             limit_usd=self.meter.limit, next_call_usd=self.margin)  # fmt: skip
            self.failure = FilmBudgetReached(self.meter.spent, self.meter.limit, self.margin)

    def result(self, model_usage: dict[str, Any]) -> None:
        """Charge what the result reports beyond what was already charged (background calls, say)."""
        for key, used in model_usage.items():
            canonical = used.get("canonicalModel")
            model = key if key in self.seen or canonical not in self.seen else canonical
            total = Usage(
                input=int(used.get("inputTokens") or 0),
                output=int(used.get("outputTokens") or 0),
                cache_read=int(used.get("cacheReadInputTokens") or 0),
                cache_write_1h=int(used.get("cacheCreationInputTokens") or 0),  # no 5m/1h split: the dearer
            )
            left = _less(total, self.seen.get(model, Usage()))
            if left != Usage():
                self._charge(canonical if model not in self.seen and canonical in PRICES else model, left)

    def unfinished(self) -> None:
        """Fail closed: a response cut off, or a request that may have been sent, is charged its worst case.

        What is charged here is counted as seen, so the result's partial usage for it isn't charged again."""
        if self.open is not None:
            model, prompt = self.open
            self.open = None
            self._charge_worst(model, prompt.prompt_tokens)
        elif self.in_flight and not self.finished:
            self._charge_worst(MODEL, MAX_CONTEXT_TOKENS)
        self.in_flight = False

    def _open_worst(self) -> Decimal:
        if self.open is None:
            return Decimal(0)
        model, prompt = self.open
        return worst_case_usd(_priced(model), prompt.prompt_tokens, MAX_TOKENS)

    def _charge(self, model: str, usage: Usage) -> None:
        self.seen[model] = self.seen.get(model, Usage()) + usage
        if model in PRICES:
            self.meter.charge_after(model, agent=AGENT, usage=usage)
        else:
            self.meter.charge_after(model, agent=AGENT, usage=usage, cost=cost_usd(DEAREST, usage),
                                    charged="dearest_price")  # fmt: skip

    def _charge_worst(self, model: str, prompt_tokens: int) -> None:
        self.seen[model] = self.seen.get(model, Usage()) + Usage(input=prompt_tokens, output=MAX_TOKENS)
        worst = worst_case_usd(_priced(model), prompt_tokens, MAX_TOKENS)
        self.meter.charge_after(model, agent=AGENT, cost=worst, charged="worst_case")


def _priced(model: str) -> str:
    return model if model in PRICES else DEAREST


def _tokens(result: ToolResult) -> int:
    """A high estimate of the prompt tokens a tool result adds."""
    images = sum(block["type"] == "image" for block in result.content)
    chars = sum(len(block.get("text", "")) for block in result.content if block["type"] != "image")
    return images * IMAGE_TOKENS + chars // CHARS_PER_TOKEN + 1


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
            session.meter.event("tool_call", agent=AGENT, tool=name, is_error=result.is_error)
            session.after_tool(result)
            return _to_sdk(result)

        return handler

    return [wrap(d["name"], d["description"], d["input_schema"]) for d in definitions]


def sdk_hooks(allowed: list[str], session: _Session) -> dict[str, list[HookMatcher]]:
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
        if session.failure is None and not session.done():
            return {}
        session.finished, session.in_flight = True, False
        return {"continue_": False, "stopReason": "the Watcher is done"}

    return {
        "PreToolUse": [HookMatcher(hooks=[only_the_watchers_tools])],
        # A tool's error result may come back as PostToolUseFailure: a failure must stop the Run either way.
        "PostToolUse": [HookMatcher(hooks=[stop_when_decided])],
        "PostToolUseFailure": [HookMatcher(hooks=[stop_when_decided])],
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
    session = _Session(meter, margin, lambda: watching.gate.accepted is not None)

    async def watch(mcp: McpTools) -> list[KeyMoment]:
        definitions = sorted([*mcp.definitions, submit_tool()], key=lambda t: t["name"])
        allowed = [tool_name(d["name"]) for d in definitions]
        server = create_sdk_mcp_server(SERVER, tools=sdk_tools(definitions, tool_gate(watching, mcp), session))
        hooks = sdk_hooks(allowed, session)
        task = load_prompt("watcher_task").format(film=film, source=source)
        result: ResultMessage | None = None
        error: ClaudeSDKError | None = None
        with tempfile.TemporaryDirectory(prefix="t2c-watcher-") as cwd:
            options = sdk_options(server=server, allowed=allowed, hooks=hooks, cap=cap, api_key=api_key, cwd=cwd)
            try:
                async for message in query(prompt=task, options=options):
                    session.start()
                    if isinstance(message, StreamEvent) and message.parent_tool_use_id is None:
                        session.stream(message.event)
                    elif isinstance(message, ResultMessage):
                        result = message
            except ClaudeSDKError as err:
                error = err
            finally:
                if result is None or result.subtype == "error_during_execution":
                    session.unfinished()  # first, so the result's partial usage for it is netted out
                if result is not None and result.model_usage:
                    session.result(dict(result.model_usage))
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
