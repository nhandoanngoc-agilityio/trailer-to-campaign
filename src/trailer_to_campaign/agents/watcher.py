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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trailer_to_campaign.agents.key_moments import KeyMoment, NotGrounded, ground
from trailer_to_campaign.agents.loop import AgentFailed, AgentLoop, McpTools, ToolResult
from trailer_to_campaign.budget import FilmMeter
from trailer_to_campaign.catalog import CastMember
from trailer_to_campaign.config import Settings

MODEL = "claude-opus-5-5"
EFFORT = "medium"
MAX_TURNS = 30
MAX_SUBMISSIONS = 3
SUBMIT = "submit_key_moments"

PROMPTS = Path(__file__).parent / "prompts"


def _prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8").strip()


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
        "description": _prompt(SUBMIT),
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
    footage: dict[str, Any] | None = None
    cast: list[CastMember] | None = None
    seen_frames: set[int] = field(default_factory=set)
    submissions: int = 0
    moments: list[KeyMoment] | None = None


async def watch_trailer(
    film: str, source: str, *, servers: list[Any], client: Any, meter: FilmMeter
) -> list[KeyMoment]:
    watching = _Watching(film, source)

    async def watch(mcp: McpTools) -> list[KeyMoment]:
        async def call_tool(name: str, arguments: dict[str, Any]) -> ToolResult:
            if name == SUBMIT:
                return _submit(watching, arguments, meter)
            if name not in mcp:
                return ToolResult.error(f"there is no tool {name!r}")
            refusal = _off_run(watching, name, arguments)
            if refusal:
                return ToolResult.error(refusal)
            result = await mcp.call(name, arguments)
            if not result.is_error:
                _remember(watching, name, arguments, result)
            return result

        loop = AgentLoop(
            client=client,
            meter=meter,
            agent="watcher",
            model=MODEL,
            effort=EFFORT,
            system=_prompt("watcher"),
            tools=[*mcp.definitions, submit_tool()],
            max_turns=MAX_TURNS,
            nudge=_prompt("watcher_nudge"),
        )
        task = _prompt("watcher_task").format(film=film, source=source)
        await loop.run([{"role": "user", "content": task}], call_tool, lambda: watching.moments is not None)
        assert watching.moments is not None
        return watching.moments

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
        else:
            watching.cast = [CastMember(c["character"], c.get("name")) for c in data["cast"]]


def _submit(watching: _Watching, arguments: dict[str, Any], meter: FilmMeter) -> ToolResult:
    if watching.footage is None or watching.cast is None:
        return ToolResult.error("call get_catalog and watch_footage, and look at frames, before submitting")
    watching.submissions += 1
    try:
        moments = ground(
            list(arguments.get("key_moments") or []), watching.footage, watching.cast, watching.seen_frames
        )
    except NotGrounded as err:
        meter.event("key_moments_rejected", agent="watcher", attempt=watching.submissions, problems=err.problems)
        if watching.submissions >= MAX_SUBMISSIONS:
            raise AgentFailed(f"the Watcher's Key Moments were not grounded after {MAX_SUBMISSIONS} tries") from err
        left = MAX_SUBMISSIONS - watching.submissions
        problems = "\n".join(f"- {p}" for p in err.problems)
        return ToolResult.error(_prompt("watcher_rejected").format(left=left, problems=problems))
    watching.moments = moments
    return ToolResult.text(f"Accepted {len(moments)} Key Moments.")
