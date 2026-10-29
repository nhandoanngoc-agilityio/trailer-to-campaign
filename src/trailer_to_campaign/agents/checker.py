"""The Checker: reviews Social Posts against the Spoiler List and the brand before Approval.

    verdicts = await check_posts(film, posts, servers=checker_servers(settings), client=client, meter=meter)

It is the only agent given the spoilers MCP server (ADR 0007), and it reads no
catalog or Key Moments: only the posts and the list. It submits one verdict per
post with a local tool; the verdicts are checked in code (judge) and sent back
until they hold, at most MAX_SUBMISSIONS times. Every problem points at the words
in the post that carry it, so a rewrite can be asked for without the Spoiler List
ever reaching the Copywriter (review.py builds those requests).
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any

from trailer_to_campaign.agents import ChecksFailed, load_prompt
from trailer_to_campaign.agents.loop import AgentLoop, McpTools, Submissions, ToolResult
from trailer_to_campaign.agents.social_posts import SocialPost
from trailer_to_campaign.budget import FilmMeter
from trailer_to_campaign.config import Settings

MODEL = "claude-opus-5-5"
EFFORT = "high"  # the real guard against spoilers: a miss here reaches the public
MAX_TURNS = 10
MAX_SUBMISSIONS = 3
SUBMIT = "submit_verdicts"
BRAND_RULES = ("clickbait", "tone", "invented_detail", "other")


class NotJudged(ChecksFailed):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("Verdicts don't hold", problems)


@dataclass(frozen=True)
class Problem:
    kind: str  # "spoiler" or "brand"
    offending_text: str  # the words in the post that carry the problem, verbatim
    reason: str  # the Checker's explanation, for the marketer only: it may describe the spoiler
    spoiler_id: str | None = None
    rule: str | None = None


@dataclass(frozen=True)
class Verdict:
    passed: bool
    problems: list[Problem]


_PROBLEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "spoiler_id", "rule", "offending_text", "reason"],
    "properties": {
        "kind": {"type": "string", "enum": ["spoiler", "brand"]},
        "spoiler_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "rule": {"anyOf": [{"type": "string", "enum": list(BRAND_RULES)}, {"type": "null"}]},
        "offending_text": {"type": "string"},
        "reason": {"type": "string"},
    },
}


def submit_tool() -> dict[str, Any]:
    verdict = {
        "type": "object",
        "additionalProperties": False,
        "required": ["post", "verdict", "problems"],
        "properties": {
            "post": {"type": "integer"},
            "verdict": {"type": "string", "enum": ["pass", "reject"]},
            "problems": {"type": "array", "items": _PROBLEM_SCHEMA},
        },
    }
    return {
        "name": SUBMIT,
        "description": load_prompt(SUBMIT),
        "strict": True,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["verdicts"],
            "properties": {"verdicts": {"type": "array", "items": verdict}},
        },
    }


def checker_servers(settings: Settings) -> list[Any]:
    """The Checker's servers: the Spoiler List only (ADR 0007)."""
    from trailer_to_campaign.tools.spoilers import create_spoiler_server

    return [create_spoiler_server(settings.films_dir.resolve())]


def judge(submitted: list[Any], posts: list[SocialPost], spoiler_ids: set[str]) -> list[Verdict]:
    """The verdicts in post order, or NotJudged listing every way they don't hold."""
    problems: list[str] = []
    by_post: dict[int, Verdict] = {}
    for raw in submitted:
        try:
            number, verdict = int(raw["post"]), str(raw["verdict"])
            found = [_problem(p) for p in raw["problems"]]
        except (KeyError, TypeError, ValueError, AttributeError):
            problems.append(
                "each verdict needs post, verdict and problems (kind, spoiler_id, rule, offending_text, reason)"
            )
            continue
        where = f"post {number}"
        if not 1 <= number <= len(posts):
            problems.append(f"{where} doesn't exist; there are posts 1 to {len(posts)}")
            continue
        if number in by_post:
            problems.append(f"{where} has more than one verdict")
            continue
        if verdict not in ("pass", "reject"):
            problems.append(f"{where}: verdict must be pass or reject")
        elif verdict == "reject" and not found:
            problems.append(f"{where}: a reject needs at least one problem")
        elif verdict == "pass" and found:
            problems.append(f"{where}: a pass has no problems; reject it if it has one")
        text = _nfc(posts[number - 1].text)
        for p in found:
            if p.kind == "spoiler" and p.spoiler_id not in spoiler_ids:
                problems.append(f"{where}: spoiler_id {p.spoiler_id!r} is not on the Spoiler List")
            if p.kind == "brand" and p.rule not in BRAND_RULES:
                problems.append(f"{where}: a brand problem needs a rule ({', '.join(BRAND_RULES)})")
            if not p.offending_text or _nfc(p.offending_text) not in text:
                problems.append(f"{where}: offending_text must be copied exactly from the post: {p.offending_text!r}")
            if not p.reason:
                problems.append(f"{where}: give a reason for each problem")
        by_post[number] = Verdict(verdict == "pass", found)
    missing = [n for n in range(1, len(posts) + 1) if n not in by_post]
    if missing:
        problems.append(f"give a verdict on every post; missing: {', '.join(f'post {n}' for n in missing)}")
    if problems:
        raise NotJudged(problems)
    return [by_post[n] for n in range(1, len(posts) + 1)]


def _problem(raw: dict[str, Any]) -> Problem:
    kind = str(raw["kind"])
    return Problem(
        kind=kind,
        offending_text=str(raw["offending_text"]).strip(),
        reason=str(raw["reason"]).strip(),
        spoiler_id=raw["spoiler_id"] if kind == "spoiler" else None,
        rule=raw["rule"] if kind == "brand" else None,
    )


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def posts_data(posts: list[SocialPost]) -> str:
    return json.dumps(
        [{"post": i, "platform": p.platform, "language": p.language, "text": p.text} for i, p in enumerate(posts, 1)],
        ensure_ascii=False,
        indent=2,
    )


@dataclass
class _Checking:
    film: str
    posts: list[SocialPost]
    gate: Submissions
    spoiler_ids: set[str] | None = None


async def check_posts(
    film: str, posts: list[SocialPost], *, servers: list[Any], client: Any, meter: FilmMeter
) -> list[Verdict]:
    gate = Submissions(meter, "checker", "verdicts", "did not hold", load_prompt("checker_rejected"), MAX_SUBMISSIONS)
    checking = _Checking(film, posts, gate)

    async def check(mcp: McpTools) -> list[Verdict]:
        async def call_tool(name: str, arguments: dict[str, Any]) -> ToolResult:
            if name == SUBMIT:
                return _submit(checking, arguments)
            if name not in mcp:
                return ToolResult.error(f"there is no tool {name!r}")
            if arguments.get("film") != checking.film:
                return ToolResult.error(f"this Run is for the Film {checking.film!r} only")
            result = await mcp.call(name, arguments)
            if not result.is_error and name == "get_spoiler_list":
                checking.spoiler_ids = {s["id"] for s in json.loads(result.content[0]["text"])["spoilers"]}
            return result

        loop = AgentLoop(
            client=client,
            meter=meter,
            agent="checker",
            model=MODEL,
            effort=EFFORT,
            system=load_prompt("checker") + "\n\n" + load_prompt("brand"),
            tools=[*mcp.definitions, submit_tool()],
            max_turns=MAX_TURNS,
            nudge=load_prompt("checker_nudge"),
        )
        task = load_prompt("checker_task").format(film=film, posts=posts_data(posts))
        await loop.run([{"role": "user", "content": task}], call_tool, lambda: gate.accepted is not None)
        assert gate.accepted is not None
        return gate.accepted

    verdicts: list[Verdict] = await McpTools.using(servers, check)
    for post, verdict in zip(posts, verdicts, strict=True):
        meter.event(
            "post_checked",
            agent="checker",
            platform=post.platform,
            language=post.language,
            verdict="pass" if verdict.passed else "reject",
            spoiler_ids=[p.spoiler_id for p in verdict.problems if p.kind == "spoiler"],
            rules=[p.rule for p in verdict.problems if p.kind == "brand"],
            text=post.text,  # the Run Log is for the team: what was checked, and what was caught in it
            problems=[asdict(p) for p in verdict.problems],
        )
    return verdicts


def _submit(checking: _Checking, arguments: dict[str, Any]) -> ToolResult:
    spoiler_ids = checking.spoiler_ids
    if spoiler_ids is None:
        return ToolResult.error("call get_spoiler_list before giving verdicts")
    submitted = list(arguments.get("verdicts") or [])
    return checking.gate.submit(lambda: judge(submitted, checking.posts, spoiler_ids))
