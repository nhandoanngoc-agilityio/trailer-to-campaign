"""The Copywriter: turns a Film's Key Moments into Social Posts in English and Vietnamese.

    posts = await write_posts(film, key_moments, servers=copywriter_servers(settings), client=client, meter=meter)

It is given the Key Moments as data and reads the Catalog Metadata through the
catalog MCP server only: never the Spoiler List (ADR 0007), so spoiler text is
never in a writer's context. It submits posts with a local tool; each submission
is traced (social_posts.trace) and sent back with its problems until it passes,
at most MAX_SUBMISSIONS times. Spoilers the trailer itself shows are the Checker's catch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from trailer_to_campaign.agents import load_prompt
from trailer_to_campaign.agents.key_moments import KeyMoment
from trailer_to_campaign.agents.loop import AgentFailed, AgentLoop, McpTools, Submissions, ToolResult
from trailer_to_campaign.agents.social_posts import ALL_PAIRS, SocialPost, post_names, trace
from trailer_to_campaign.budget import FilmMeter
from trailer_to_campaign.config import Settings

MODEL = "claude-opus-5-5"
EFFORT = "medium"
MAX_TURNS = 12
MAX_SUBMISSIONS = 3
SUBMIT = "submit_social_posts"

_POST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["platform", "language", "text", "claims"],
    "properties": {
        "platform": {"type": "string", "enum": ["facebook", "instagram", "tiktok"]},
        "language": {"type": "string", "enum": ["en", "vi"]},
        "text": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "source"],
                "properties": {"text": {"type": "string"}, "source": {"type": "string"}},
            },
        },
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
            "required": ["posts"],
            "properties": {"posts": {"type": "array", "items": _POST_SCHEMA}},
        },
    }


def copywriter_servers(settings: Settings) -> list[Any]:
    """The Copywriter's servers: the catalog only. Never the spoilers server (ADR 0007)."""
    from trailer_to_campaign.tools.catalog import create_catalog_server

    return [create_catalog_server(settings.films_dir.resolve())]


@dataclass(frozen=True)
class Rewrite:
    """A post the Checker rejected, and why, in words the Copywriter may see (never the Spoiler List)."""

    post: SocialPost
    reasons: list[str]


@dataclass
class _Writing:
    film: str
    key_moments: list[KeyMoment]
    required: list[tuple[str, str]]
    gate: Submissions
    catalog: dict[str, Any] | None = None


def key_moments_data(key_moments: list[KeyMoment]) -> str:
    """The Key Moments as the Copywriter reads them: numbered so a claim can cite key_moment:N."""
    return json.dumps(
        [
            {
                "key_moment": i,
                "start": m.start,
                "end": m.end,
                "what_happens": m.what_happens,
                "mood": m.mood,
                "quote": m.quote.text if m.quote else None,
                "characters": m.characters,
            }
            for i, m in enumerate(key_moments, start=1)
        ],
        ensure_ascii=False,
        indent=2,
    )


async def write_posts(
    film: str, key_moments: list[KeyMoment], *, servers: list[Any], client: Any, meter: FilmMeter
) -> list[SocialPost]:
    """The first draft: all six posts."""
    task = load_prompt("copywriter_task").format(film=film, key_moments=key_moments_data(key_moments))
    return await _write(film, key_moments, list(ALL_PAIRS), task, servers=servers, client=client, meter=meter)


async def revise_posts(
    film: str,
    key_moments: list[KeyMoment],
    rewrites: list[Rewrite],
    *,
    servers: list[Any],
    client: Any,
    meter: FilmMeter,
) -> list[SocialPost]:
    """A rewrite of only the rejected posts; the posts that passed are not shown or touched."""
    data = json.dumps(
        [
            {"platform": r.post.platform, "language": r.post.language, "text": r.post.text, "reasons": r.reasons}
            for r in rewrites
        ],
        ensure_ascii=False,
        indent=2,
    )
    task = load_prompt("copywriter_revise_task").format(
        film=film, key_moments=key_moments_data(key_moments), rewrites=data
    )
    required = [(r.post.platform, r.post.language) for r in rewrites]
    return await _write(film, key_moments, required, task, servers=servers, client=client, meter=meter)


async def _write(
    film: str,
    key_moments: list[KeyMoment],
    required: list[tuple[str, str]],
    task: str,
    *,
    servers: list[Any],
    client: Any,
    meter: FilmMeter,
) -> list[SocialPost]:
    # A rejection names every post still asked for, so it can't be read as only the one at fault (T18).
    rejected = load_prompt("copywriter_rejected").replace("{required}", post_names(required))
    gate = Submissions(meter, "copywriter", "Social Posts", "were not traced", rejected, MAX_SUBMISSIONS)
    writing = _Writing(film, key_moments, required, gate)

    async def write(mcp: McpTools) -> list[SocialPost]:
        async def call_tool(name: str, arguments: dict[str, Any]) -> ToolResult:
            if name == SUBMIT:
                return _submit(writing, arguments)
            if name not in mcp:
                return ToolResult.error(f"there is no tool {name!r}")
            if arguments.get("film") != writing.film:
                return ToolResult.error(f"this Run is for the Film {writing.film!r} only")
            result = await mcp.call(name, arguments)
            if not result.is_error and name == "get_catalog":
                try:
                    writing.catalog = json.loads(result.content[0]["text"])
                except (IndexError, KeyError, ValueError) as err:
                    raise AgentFailed(f"the Copywriter could not read the catalog ({type(err).__name__})") from err
            return result

        loop = AgentLoop(
            client=client,
            meter=meter,
            agent="copywriter",
            model=MODEL,
            effort=EFFORT,
            system=load_prompt("copywriter") + "\n\n" + load_prompt("brand"),
            tools=[*mcp.definitions, submit_tool()],
            max_turns=MAX_TURNS,
            nudge=load_prompt("copywriter_nudge"),
        )
        await loop.run([{"role": "user", "content": task}], call_tool, lambda: gate.accepted is not None)
        assert gate.accepted is not None
        return gate.accepted

    return await McpTools.using(servers, write)


def _submit(writing: _Writing, arguments: dict[str, Any]) -> ToolResult:
    catalog = writing.catalog
    if catalog is None:
        return ToolResult.error("call get_catalog before submitting")
    posts = list(arguments.get("posts") or [])
    return writing.gate.submit(
        lambda: trace(posts, writing.key_moments, catalog, writing.required), submitted=arguments
    )
