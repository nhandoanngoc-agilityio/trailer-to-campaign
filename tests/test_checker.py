"""Tests for the Checker's loop, against the real spoilers MCP server and a scripted Claude client."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fake_claude import FakeClient, Step, reply, results, use
from mcp import Client

from trailer_to_campaign.agents import load_prompt
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
from trailer_to_campaign.tools.catalog import create_catalog_server
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


def read_both(params: dict[str, Any]) -> Any:
    return reply(use("get_spoiler_list", film="sample"), use("get_catalog", film="sample"))


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
        self.servers = [create_spoiler_server(films), create_catalog_server(films)]
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
    verdicts, fake = await run.check([read_both, submit(GOOD)])

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
    # The Run Log is for the team: it keeps the post as checked and what was caught in it, in full.
    assert checked[1]["text"] == TRAP.text
    assert checked[1]["problems"] == [SPOILER]
    task = fake.requests[0]["messages"][0]["content"]
    assert '"post": 2' in task and "carry on with the procedure" in task  # the posts, as numbered data


async def test_the_checker_is_offered_the_spoiler_list_and_the_catalog(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    _, fake = await run.check([read_both, submit(GOOD)])
    for request in fake.requests:
        assert {t["name"] for t in request["tools"]} == {"get_spoiler_list", "get_catalog", "submit_verdicts"}


async def test_the_real_server_set_is_the_spoiler_list_and_the_catalog(films: Path) -> None:
    names = set()
    for server in checker_servers(Settings(films_dir=films)):
        async with Client(server) as client:
            names |= {t.name for t in (await client.list_tools()).tools}
    assert names == {"get_spoiler_list", "get_catalog"}


async def test_verdicts_before_reading_the_spoiler_list_are_refused(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    _, fake = await run.check([submit(GOOD, 0), read_both, submit(GOOD)])
    [refused] = results(fake.requests[1])
    assert refused["is_error"] and "get_spoiler_list" in refused["content"][0]["text"]


# T15: the Checker checks names against the Film's cast, so it must have read it before it judges.


@pytest.mark.parametrize(
    "first",
    [
        lambda params: reply(use("get_spoiler_list", film="sample")),
        lambda params: reply(use("get_catalog", film="sample")),
    ],
)
async def test_verdicts_before_reading_both_are_refused_and_not_counted(
    films: Path, tmp_path: Path, first: Step
) -> None:
    run = Run(films, tmp_path)
    _, fake = await run.check([first, submit(GOOD, 0), read_both, submit(GOOD, 1)])

    [refused] = results(fake.requests[2])
    text = refused["content"][0]["text"]
    assert refused["is_error"] and "get_spoiler_list" in text and "get_catalog" in text
    assert run.events("verdicts_rejected") == []


async def test_the_checker_is_shown_the_films_cast(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    _, fake = await run.check([read_both, submit(GOOD)])

    shown = json.dumps(results(fake.requests[1]))
    assert "cast" in shown and "Agent 327" in shown


async def test_a_catalog_that_cannot_be_read_never_lets_a_post_pass(films: Path, tmp_path: Path) -> None:
    from mcp.server.mcpserver import MCPServer

    broken = MCPServer("catalog")

    @broken.tool()
    def get_catalog(film: str) -> dict:
        raise ValueError("the film.toml is damaged")

    run = Run(films, tmp_path)
    run.servers = [create_spoiler_server(films), broken]
    with pytest.raises(AgentFailed, match="catalog"):
        await run.check([read_both, submit(GOOD)])  # fail closed: these posts go to a person, flagged
    assert run.events("post_checked") == []
    assert len(run.events("model_call")) == 1  # it can't judge without the cast, so no more paid turns


@pytest.mark.parametrize(
    "answer",
    [
        {"film": "sample"},
        {"film": "sample", "cast": []},
        {"film": "sample", "cast": None},
        {"film": "sample", "cast": "unknown"},
        {"film": "sample", "cast": {"x": 1}},
        {"film": "sample", "cast": [{}]},
        {"film": "sample", "cast": [{"name": "No Character"}]},
    ],
)
async def test_a_catalog_with_no_cast_never_opens_the_gate(films: Path, tmp_path: Path, answer: dict[str, Any]) -> None:
    from mcp.server.mcpserver import MCPServer

    broken = MCPServer("catalog")

    @broken.tool()
    def get_catalog(film: str) -> dict:
        return answer

    run = Run(films, tmp_path)
    run.servers = [create_spoiler_server(films), broken]
    with pytest.raises(AgentFailed, match="catalog"):
        await run.check([read_both, submit(GOOD)])
    assert run.events("post_checked") == []


async def test_a_spoiler_list_tool_error_fails_at_once(films: Path, tmp_path: Path) -> None:
    from mcp.server.mcpserver import MCPServer

    broken = MCPServer("spoilers")

    @broken.tool()
    def get_spoiler_list(film: str) -> dict:
        raise ValueError("the film.toml is damaged")

    run = Run(films, tmp_path)
    run.servers = [broken, create_catalog_server(films)]
    with pytest.raises(AgentFailed, match="Spoiler List"):
        await run.check([read_both, submit(GOOD)])
    assert len(run.events("model_call")) == 1


@pytest.mark.parametrize("reads", [("get_spoiler_list", "get_catalog"), ("get_catalog", "get_spoiler_list")])
async def test_verdicts_in_the_same_reply_as_the_reads_are_refused(
    films: Path, tmp_path: Path, reads: tuple[str, str]
) -> None:
    """They were written before the model saw the Spoiler List or the cast, so they can't be judgments of either."""
    run = Run(films, tmp_path)
    blind = [{"post": 1, "verdict": "pass", "problems": []}, {"post": 2, "verdict": "pass", "problems": []}]
    all_at_once = lambda params: reply(  # noqa: E731
        use(reads[0], film="sample"), use(reads[1], film="sample"), use("submit_verdicts", 0, verdicts=blind)
    )
    verdicts, fake = await run.check([all_at_once, submit(GOOD)])

    refused = results(fake.requests[1])[-1]
    assert refused["is_error"] and "get_catalog" in refused["content"][0]["text"]
    assert run.events("verdicts_rejected") == []  # not counted: it never got to be judged
    assert verdicts[1].passed is False  # what counts is the verdict given after reading


async def test_a_second_read_in_the_same_reply_as_verdicts_does_not_undo_the_first(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    read_again_and_judge = lambda params: reply(  # noqa: E731
        use("get_catalog", 2, film="sample"), use("submit_verdicts", 0, verdicts=GOOD)
    )
    verdicts, fake = await run.check([read_both, read_again_and_judge])

    assert len(fake.requests) == 2  # accepted in that reply: both reads had already reached the model
    assert verdicts[1].passed is False and len(run.events("post_checked")) == 2


async def test_a_failed_read_after_a_good_one_does_not_lose_the_review(films: Path, tmp_path: Path) -> None:
    """Once the model has both, a later failed read costs nothing: the verdicts are what was paid for."""
    from mcp.server.mcpserver import MCPServer

    flaky = MCPServer("catalog")
    answers = [{"film": "sample", "cast": [{"character": "Agent 327", "name": None}]}]

    @flaky.tool()
    def get_catalog(film: str) -> dict:
        if answers:
            return answers.pop()
        raise ValueError("the film.toml is being saved")

    run = Run(films, tmp_path)
    run.servers = [create_spoiler_server(films), flaky]
    read_again_and_judge = lambda params: reply(  # noqa: E731
        use("get_catalog", 2, film="sample"), use("submit_verdicts", 0, verdicts=GOOD)
    )
    verdicts, _ = await run.check([read_both, read_again_and_judge])
    assert verdicts[1].passed is False


async def test_a_failed_read_says_why_and_stays_in_the_run_log(films: Path, tmp_path: Path) -> None:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    broken = MCPServer("catalog")

    @broken.tool()
    def get_catalog(film: str) -> dict:
        raise ToolError("the Film's catalog has no title")

    run = Run(films, tmp_path)
    run.servers = [create_spoiler_server(films), broken]
    with pytest.raises(AgentFailed, match="no title"):  # a person reads this on the flag
        await run.check([read_both, submit(GOOD)])
    calls = run.events("tool_call")
    assert {"get_catalog": True, "get_spoiler_list": False} == {c["tool"]: c["is_error"] for c in calls}


@pytest.mark.parametrize("given", ["spoilers", "catalog"])
async def test_a_checker_not_given_both_tools_fails_before_any_paid_call(
    films: Path, tmp_path: Path, given: str
) -> None:
    run = Run(films, tmp_path)
    run.servers = [create_spoiler_server(films) if given == "spoilers" else create_catalog_server(films)]
    with pytest.raises(AgentFailed, match="not given"):
        await run.check([read_both, submit(GOOD)])
    assert run.events("model_call") == []


async def test_a_read_unseen_when_verdicts_are_given_still_shuts_the_gate(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    spoilers = lambda params: reply(use("get_spoiler_list", film="sample"))  # noqa: E731
    catalog_and_verdicts = lambda params: reply(  # noqa: E731
        use("get_catalog", film="sample"), use("submit_verdicts", 0, verdicts=GOOD)
    )
    _, fake = await run.check([spoilers, catalog_and_verdicts, submit(GOOD)])

    assert results(fake.requests[2])[-1]["is_error"]  # the cast was still unseen


async def test_another_films_catalog_is_refused(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    other = lambda params: reply(use("get_catalog", film="another-film"))  # noqa: E731
    _, fake = await run.check([other, read_both, submit(GOOD)])

    [refused] = results(fake.requests[1])
    assert refused["is_error"] and "sample" in refused["content"][0]["text"]


def test_the_prompt_asks_for_the_cast_check() -> None:
    prompt = load_prompt("checker")
    assert "get_catalog" in prompt and "invented_detail" in prompt
    assert "cast" in prompt.casefold() and "null" in prompt  # the wording is the Eval Set's to judge
    assert "get_catalog" in load_prompt("checker_nudge")  # or the nudge sends it to a refusal
    cast_rule = next(line for line in prompt.splitlines() if line.startswith("Cast:"))
    assert "hashtag" in cast_rule  # not only "its own words": no code compares a hashtag with the cast
    assert "code has checked" not in cast_rule  # it can't vouch for every quote style, or for a marketer's edit


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
    result, fake = await run.check([read_both, submit(verdicts), submit(GOOD, 2)])
    [refused] = results(fake.requests[2])
    assert refused["is_error"] and says in refused["content"][0]["text"]
    assert result[1].passed is False


async def test_rejected_verdicts_are_not_logged_as_a_submission(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)  # T18 logs the writers' rejected drafts; the Checker passes no submission
    invented = {"post": 2, "verdict": "reject", "problems": [problem(offending_text="he is captured")]}
    await run.check([read_both, submit([GOOD[0], invented]), submit(GOOD, 2)])

    [event] = run.events("verdicts_rejected")
    assert "submitted" not in event
    assert SPOILER["reason"] not in json.dumps(event)  # the verdict's reason stays out
    assert any("he is captured" in p for p in event["problems"])  # but a problem can quote its offending_text


async def test_the_checker_gives_up_after_too_many_bad_submissions(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    bad = [submit([GOOD[0]], i) for i in range(MAX_SUBMISSIONS)]
    with pytest.raises(AgentFailed):
        await run.check([read_both, *bad])


async def test_offending_text_matches_in_either_unicode_form(films: Path, tmp_path: Path) -> None:
    import unicodedata

    vi = SocialPost("tiktok", "vi", unicodedata.normalize("NFD", f"{TITLE}: bí mật cuối phim. Xem ngay"), CLAIMS)
    spoiler = problem(offending_text=unicodedata.normalize("NFC", "bí mật cuối phim"))
    run = Run(films, tmp_path)
    verdicts, _ = await run.check(
        [read_both, submit([{"post": 1, "verdict": "reject", "problems": [spoiler]}])], posts=[vi]
    )
    assert not verdicts[0].passed


async def test_a_spoiler_list_that_cannot_be_read_fails_the_checker_not_the_run(films: Path, tmp_path: Path) -> None:
    from mcp.server.mcpserver import MCPServer

    broken = MCPServer("spoilers")

    @broken.tool()
    def get_spoiler_list(film: str) -> dict:
        return {"film": film, "items": []}  # not the shape the Checker reads

    run_log = RunLog.create(tmp_path / "runs", "sample")
    client = FakeClient([read_both, submit(GOOD)])
    servers = [broken, create_catalog_server(films)]
    with pytest.raises(AgentFailed, match="Spoiler List"):
        await check_posts("sample", POSTS, servers=servers, client=client, meter=FilmMeter(Decimal("2"), run_log))
