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
from pathlib import Path
from typing import Any

from trailer_to_campaign.agents.key_moments import KeyMoment
from trailer_to_campaign.agents.loop import AgentFailed, AgentLoop, McpTools, ToolResult
from trailer_to_campaign.agents.social_posts import NotTraced, SocialPost, trace
from trailer_to_campaign.budget import FilmMeter
from trailer_to_campaign.config import Settings

MODEL = "claude-opus-5-5"
EFFORT = "medium"
MAX_TURNS = 12
MAX_SUBMISSIONS = 3
SUBMIT = "submit_social_posts"

PROMPTS = Path(__file__).parent / "prompts"


def _prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8").strip()


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
        "description": _prompt(SUBMIT),
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


@dataclass
class _Writing:
    film: str
    key_moments: list[KeyMoment]
    catalog: dict[str, Any] | None = None
    submissions: int = 0
    posts: list[SocialPost] | None = None


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
    writing = _Writing(film, key_moments)

    async def write(mcp: McpTools) -> list[SocialPost]:
        async def call_tool(name: str, arguments: dict[str, Any]) -> ToolResult:
            if name == SUBMIT:
                return _submit(writing, arguments, meter)
            if name not in mcp:
                return ToolResult.error(f"there is no tool {name!r}")
            if arguments.get("film") != writing.film:
                return ToolResult.error(f"this Run is for the Film {writing.film!r} only")
            result = await mcp.call(name, arguments)
            if not result.is_error and name == "get_catalog":
                writing.catalog = json.loads(result.content[0]["text"])
            return result

        loop = AgentLoop(
            client=client,
            meter=meter,
            agent="copywriter",
            model=MODEL,
            effort=EFFORT,
            system=_prompt("copywriter"),
            tools=[*mcp.definitions, submit_tool()],
            max_turns=MAX_TURNS,
            nudge=_prompt("copywriter_nudge"),
        )
        task = _prompt("copywriter_task").format(film=film, key_moments=key_moments_data(key_moments))
        await loop.run([{"role": "user", "content": task}], call_tool, lambda: writing.posts is not None)
        assert writing.posts is not None
        return writing.posts

    return await McpTools.using(servers, write)


def _submit(writing: _Writing, arguments: dict[str, Any], meter: FilmMeter) -> ToolResult:
    if writing.catalog is None:
        return ToolResult.error("call get_catalog before submitting")
    writing.submissions += 1
    try:
        posts = trace(list(arguments.get("posts") or []), writing.key_moments, writing.catalog)
    except NotTraced as err:
        meter.event("social_posts_rejected", agent="copywriter", attempt=writing.submissions, problems=err.problems)
        if writing.submissions >= MAX_SUBMISSIONS:
            raise AgentFailed(f"the Copywriter's Social Posts were not traced after {MAX_SUBMISSIONS} tries") from err
        left = MAX_SUBMISSIONS - writing.submissions
        problems = "\n".join(f"- {p}" for p in err.problems)
        return ToolResult.error(_prompt("copywriter_rejected").format(left=left, problems=problems))
    writing.posts = posts
    return ToolResult.text(f"Accepted {len(posts)} Social Posts.")
