"""The Watcher: studies a Film's trailer and produces its Key Moments.

    moments = await watch_trailer(film, source, servers=watcher_servers(settings), client=client, meter=meter)

It reads the Catalog Metadata, the trailer's timeline (frames and transcript) and
the frames it chooses, through the footage and catalog MCP servers only: never
the Spoiler List (ADR 0007). It submits Key Moments with a local tool; each
submission is grounded (key_moments.ground) and sent back with its problems
until it passes, at most MAX_SUBMISSIONS times.

Every tool call is pinned to this Run's Film and trailer, so text in the footage
or the catalog can't steer the Watcher to another Film or another link.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from trailer_to_campaign.agents import load_prompt
from trailer_to_campaign.agents.key_moments import KeyMoment, ground
from trailer_to_campaign.agents.loop import AgentLoop, McpTools, Submissions, ToolResult
from trailer_to_campaign.budget import FilmMeter
from trailer_to_campaign.catalog import CastMember
from trailer_to_campaign.config import Settings

MODEL = "claude-opus-5-5"
EFFORT = "medium"
MAX_TURNS = 30
MAX_SUBMISSIONS = 3
SUBMIT = "submit_key_moments"

_MOMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["start", "end", "what_happens", "mood", "quote", "characters", "frames"],
    "properties": {
        "start": {"type": "number"},
        "end": {"type": "number"},
        "what_happens": {"type": "string"},
        "mood": {"type": "string"},
        "quote": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "characters": {"type": "array", "items": {"type": "string"}},
        "frames": {"type": "array", "items": {"type": "integer"}},
    },
}


def submit_tool() -> dict[str, Any]:
    return {
        "name": SUBMIT,
        "description": load_prompt(SUBMIT),
        "strict": True,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["key_moments"],
            "properties": {"key_moments": {"type": "array", "items": _MOMENT_SCHEMA}},
        },
    }


def watcher_servers(settings: Settings) -> list[Any]:
    """The Watcher's servers: footage and catalog. Never the spoilers server (ADR 0007)."""
    from trailer_to_campaign.footage.sources import YtDlpDownloader
    from trailer_to_campaign.footage.transcript import FasterWhisperTranscriber
    from trailer_to_campaign.tools.catalog import create_catalog_server
    from trailer_to_campaign.tools.footage import create_server

    films_dir = settings.films_dir.resolve()
    return [
        create_server(
            films_dir=films_dir,
            downloader=YtDlpDownloader(),
            transcriber=FasterWhisperTranscriber(settings.whisper_model),
        ),
        create_catalog_server(films_dir),
    ]


@dataclass
class _Watching:
    """What the Watcher has been shown so far: the grounding for its Key Moments."""

    film: str
    source: str
    gate: Submissions
    footage: dict[str, Any] | None = None
    cast: list[CastMember] | None = None
    seen_frames: set[int] = field(default_factory=set)


def start_watching(film: str, source: str, meter: FilmMeter) -> _Watching:
    gate = Submissions(
        meter, "watcher", "Key Moments", "were not grounded", load_prompt("watcher_rejected"), MAX_SUBMISSIONS
    )
    return _Watching(film, source, gate)


def tool_gate(watching: _Watching, mcp: McpTools) -> Callable[[str, dict[str, Any]], Awaitable[ToolResult]]:
    """The one way the Watcher reaches a tool, whichever harness runs it (v0's loop or the Agent SDK).

    Every call is pinned to this Run's Film and trailer, what the Watcher is shown is
    remembered as grounding, and a submission is grounded before it is accepted.
    """

    async def call_tool(name: str, arguments: dict[str, Any]) -> ToolResult:
        if name == SUBMIT:
            return _submit(watching, arguments)
        if name not in mcp:
            return ToolResult.error(f"there is no tool {name!r}")
        refusal = _off_run(watching, name, arguments)
        if refusal:
            return ToolResult.error(refusal)
        result = await mcp.call(name, arguments)
        if not result.is_error:
            _remember(watching, name, arguments, result)
        return result

    return call_tool


async def watch_trailer(
    film: str, source: str, *, servers: list[Any], client: Any, meter: FilmMeter
) -> list[KeyMoment]:
    watching = start_watching(film, source, meter)
    gate = watching.gate

    async def watch(mcp: McpTools) -> list[KeyMoment]:
        call_tool = tool_gate(watching, mcp)
        loop = AgentLoop(
            client=client,
            meter=meter,
            agent="watcher",
            model=MODEL,
            effort=EFFORT,
            system=load_prompt("watcher"),
            tools=[*mcp.definitions, submit_tool()],
            max_turns=MAX_TURNS,
            nudge=load_prompt("watcher_nudge"),
        )
        task = load_prompt("watcher_task").format(film=film, source=source)
        await loop.run([{"role": "user", "content": task}], call_tool, lambda: gate.accepted is not None)
        assert gate.accepted is not None
        return gate.accepted

    return await McpTools.using(servers, watch)


def _off_run(watching: _Watching, name: str, arguments: dict[str, Any]) -> str | None:
    """Why a tool call is outside this Run's Film and trailer, if it is."""
    if arguments.get("film") != watching.film:
        return f"this Run is for the Film {watching.film!r} only"
    if name == "watch_footage" and arguments.get("source") != watching.source:
        return f"this Run watches the trailer {watching.source!r} only"
    if name == "get_frame":
        if watching.footage is None:
            return "call watch_footage first"
        if arguments.get("footage_id") != watching.footage.get("footage_id"):
            return f"use footage_id {watching.footage.get('footage_id')!r} from watch_footage"
    return None


def _remember(watching: _Watching, name: str, arguments: dict[str, Any], result: ToolResult) -> None:
    if name == "get_frame":
        watching.seen_frames.add(int(arguments["index"]))
    elif name in ("watch_footage", "get_catalog"):
        data = json.loads(result.content[0]["text"])
        if name == "watch_footage":
            watching.footage = data
            watching.gate.meter.event("footage_read", source=watching.source, footage_id=data["footage_id"])
        else:
            watching.cast = [CastMember(c["character"], c.get("name")) for c in data["cast"]]


def _submit(watching: _Watching, arguments: dict[str, Any]) -> ToolResult:
    footage, cast = watching.footage, watching.cast
    if footage is None or cast is None:
        return ToolResult.error("call get_catalog and watch_footage, and look at frames, before submitting")
    moments = list(arguments.get("key_moments") or [])
    return watching.gate.submit(lambda: ground(moments, footage, cast, watching.seen_frames), submitted=arguments)
