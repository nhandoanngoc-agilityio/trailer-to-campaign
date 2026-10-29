"""Tests for the Checker's loop, against the real spoilers MCP server and a scripted Claude client."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fake_claude import FakeClient, Step, reply, results, use
from mcp import Client

from trailer_to_campaign.agents.checker import (
    MAX_SUBMISSIONS,
    MODEL,
    Problem,
    Verdict,
    check_posts,
    checker_servers,
)
from trailer_to_campaign.agents.loop import AgentFailed
from trailer_to_campaign.agents.social_posts import Claim, SocialPost
from trailer_to_campaign.budget import FilmMeter
from trailer_to_campaign.config import Settings
from trailer_to_campaign.run_log import RunLog
from trailer_to_campaign.tools.spoilers import create_spoiler_server

pytestmark = pytest.mark.anyio

TITLE = "Agent 327: Operation Barbershop"
FILM_TOML = f"""
[catalog]
title = "{TITLE}"
synopsis = "Secret agent 327 follows a clue to a barbershop in Amsterdam."
genre = ["animation"]
language = "en"
runtime_minutes = 4

[[catalog.cast]]
character = "Agent 327"

[[spoilers]]
id = "ending-capture"
text = "The film ends with Agent 327 captured, to be the villains' next recruit."
"""
CLAIMS = [Claim("The title", "catalog:title")]
CLEAN = SocialPost("facebook", "en", f'{TITLE}: "Just a trim, please." Stream it now.', CLAIMS)
TRAP = SocialPost("tiktok", "en", f'{TITLE}: "Doctor, carry on with the procedure." Stream it now.', CLAIMS)
POSTS = [CLEAN, TRAP]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def films(tmp_path: Path) -> Path:
    film = tmp_path / "films" / "sample"
    film.mkdir(parents=True)
    (film / "film.toml").write_text(FILM_TOML, encoding="utf-8")
    return tmp_path / "films"


def read_spoilers(params: dict[str, Any]) -> Any:
    return reply(use("get_spoiler_list", film="sample"))


SPOILER = {
    "kind": "spoiler",
    "spoiler_id": "ending-capture",
    "rule": None,
    "offending_text": "carry on with the procedure",
    "reason": "Points at the ending, where the agent is captured.",
}
GOOD = [
    {"post": 1, "verdict": "pass", "problems": []},
    {"post": 2, "verdict": "reject", "problems": [SPOILER]},
]


def submit(verdicts: list[dict[str, Any]], n: int = 1) -> Step:
    return lambda params: reply(use("submit_verdicts", n, verdicts=verdicts))


class Run:
    def __init__(self, films: Path, tmp_path: Path) -> None:
        self.servers = [create_spoiler_server(films)]
        self.run_log = RunLog.create(tmp_path / "runs", "sample")
        self.meter = FilmMeter(Decimal("2.00"), self.run_log)

    async def check(self, steps: list[Step], posts: list[SocialPost] = POSTS) -> tuple[Any, Any]:
        client = FakeClient(steps)
        verdicts = await check_posts("sample", posts, servers=self.servers, client=client, meter=self.meter)
        return verdicts, client.messages

    def events(self, kind: str) -> list[dict[str, Any]]:
        lines = [json.loads(line) for line in self.run_log.path.read_text().splitlines()]
        return [line for line in lines if line["event"] == kind]


async def test_the_checker_returns_a_verdict_for_each_post(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    verdicts, fake = await run.check([read_spoilers, submit(GOOD)])

    assert verdicts == [
        Verdict(passed=True, problems=[]),
        Verdict(
            passed=False,
            problems=[
                Problem("spoiler", "carry on with the procedure", SPOILER["reason"], spoiler_id="ending-capture")
            ],
        ),
    ]
    calls = run.events("model_call")
    assert len(calls) == 2 and all(c["model"] == MODEL and c["agent"] == "checker" for c in calls)
    assert fake.requests[0]["output_config"] == {"effort": "high"}
    checked = run.events("post_checked")
    assert [(c["platform"], c["verdict"], c["spoiler_ids"]) for c in checked] == [
        ("facebook", "pass", []),
        ("tiktok", "reject", ["ending-capture"]),
    ]
    task = fake.requests[0]["messages"][0]["content"]
    assert '"post": 2' in task and "carry on with the procedure" in task  # the posts, as numbered data


async def test_the_checker_is_offered_only_the_spoiler_list(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    _, fake = await run.check([read_spoilers, submit(GOOD)])
    for request in fake.requests:
        assert [t["name"] for t in request["tools"]] == ["get_spoiler_list", "submit_verdicts"]


async def test_the_real_server_set_is_the_spoiler_list_only(films: Path) -> None:
    names = set()
    for server in checker_servers(Settings(films_dir=films)):
        async with Client(server) as client:
            names |= {t.name for t in (await client.list_tools()).tools}
    assert names == {"get_spoiler_list"}


async def test_verdicts_before_reading_the_spoiler_list_are_refused(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    _, fake = await run.check([submit(GOOD, 0), read_spoilers, submit(GOOD)])
    [refused] = results(fake.requests[1])
    assert refused["is_error"] and "get_spoiler_list" in refused["content"][0]["text"]


def problem(**over: Any) -> dict[str, Any]:
    return {**SPOILER, **over}


@pytest.mark.parametrize(
    ("verdicts", "says"),
    [
        ([GOOD[0]], "post 2"),
        ([*GOOD, {"post": 3, "verdict": "pass", "problems": []}], "post 3"),
        ([GOOD[0], {"post": 2, "verdict": "reject", "problems": []}], "at least one problem"),
        ([{"post": 1, "verdict": "pass", "problems": [problem()]}, GOOD[1]], "no problems"),
        ([GOOD[0], {"post": 2, "verdict": "reject", "problems": [problem(spoiler_id="the-twist")]}], "the-twist"),
        ([GOOD[0], {"post": 2, "verdict": "reject", "problems": [problem(offending_text="he is captured")]}], "post"),
        (
            [GOOD[0], {"post": 2, "verdict": "reject", "problems": [problem(kind="brand", spoiler_id=None)]}],
            "rule",
        ),
    ],
)
async def test_the_gate_sends_back_verdicts_that_do_not_hold(
    films: Path, tmp_path: Path, verdicts: list[Any], says: str
) -> None:
    run = Run(films, tmp_path)
    result, fake = await run.check([read_spoilers, submit(verdicts), submit(GOOD, 2)])
    [refused] = results(fake.requests[2])
    assert refused["is_error"] and says in refused["content"][0]["text"]
    assert result[1].passed is False


async def test_the_checker_gives_up_after_too_many_bad_submissions(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    bad = [submit([GOOD[0]], i) for i in range(MAX_SUBMISSIONS)]
    with pytest.raises(AgentFailed):
        await run.check([read_spoilers, *bad])


async def test_offending_text_matches_in_either_unicode_form(films: Path, tmp_path: Path) -> None:
    import unicodedata

    vi = SocialPost("tiktok", "vi", unicodedata.normalize("NFD", f"{TITLE}: bí mật cuối phim. Xem ngay"), CLAIMS)
    spoiler = problem(offending_text=unicodedata.normalize("NFC", "bí mật cuối phim"))
    run = Run(films, tmp_path)
    verdicts, _ = await run.check(
        [read_spoilers, submit([{"post": 1, "verdict": "reject", "problems": [spoiler]}])], posts=[vi]
    )
    assert not verdicts[0].passed
