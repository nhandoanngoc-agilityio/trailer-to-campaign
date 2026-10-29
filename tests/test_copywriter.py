"""Tests for the Copywriter's loop, against the real catalog MCP server and a scripted Claude client."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fake_claude import FakeClient, Step, reply, results, use
from mcp import Client

from trailer_to_campaign.agents.copywriter import MAX_SUBMISSIONS, MODEL, copywriter_servers, write_posts
from trailer_to_campaign.agents.key_moments import KeyMoment, Quote
from trailer_to_campaign.agents.loop import AgentFailed
from trailer_to_campaign.budget import FilmMeter
from trailer_to_campaign.config import Settings
from trailer_to_campaign.run_log import RunLog
from trailer_to_campaign.tools.catalog import create_catalog_server

pytestmark = pytest.mark.anyio

TITLE = "Agent 327: Operation Barbershop"
FILM_TOML = f"""
[catalog]
title = "{TITLE}"
synopsis = "Secret agent 327 follows a clue to a barbershop in Amsterdam."
genre = ["animation", "comedy"]
language = "en"
runtime_minutes = 4

[[catalog.cast]]
character = "Agent 327"

[[spoilers]]
id = "ending"
text = "The secret procedure happens at the end."
"""
SPOILER = "secret procedure"
MOMENTS = [
    KeyMoment(
        10.0, 16.0, "Agent 327 reports in.", "cool", Quote("This is 327. I'm going in.", 12.6, 15.4), ["Agent 327"], [5]
    ),
    KeyMoment(
        84.0, 90.0, "A cape is drawn up.", "deadpan", Quote("Just a trim, please.", 86.0, 88.0), ["Agent 327"], [34]
    ),
    KeyMoment(92.0, 118.0, "A brawl in the barbershop.", "slapstick", None, ["Agent 327"], [36]),
]
EN = f'{TITLE}: a spy walks into a barbershop. "Just a trim, please." Watch it now.'
VI = f'{TITLE}: điệp viên bước vào tiệm cắt tóc. "Just a trim, please." (tạm dịch: Chỉ tỉa thôi.) Xem ngay.'


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def films(tmp_path: Path) -> Path:
    film = tmp_path / "films" / "sample"
    film.mkdir(parents=True)
    (film / "film.toml").write_text(FILM_TOML, encoding="utf-8")
    return tmp_path / "films"


def six_posts(**tiktok_en: Any) -> list[dict[str, Any]]:
    claims = [
        {"text": "He asks for a trim", "source": "key_moment:2"},
        {"text": "The title", "source": "catalog:title"},
    ]
    posts = [
        {"platform": p, "language": lang, "text": EN if lang == "en" else VI, "claims": claims}
        for p in ("facebook", "instagram", "tiktok")
        for lang in ("en", "vi")
    ]
    posts[4] = {**posts[4], **tiktok_en}
    return posts


def read_catalog(params: dict[str, Any]) -> Any:
    return reply(use("get_catalog", film="sample"))


def submit(n: int = 1, **tiktok_en: Any) -> Step:
    return lambda params: reply(use("submit_social_posts", n, posts=six_posts(**tiktok_en)))


class Run:
    def __init__(self, films: Path, tmp_path: Path) -> None:
        self.servers = [create_catalog_server(films)]
        self.run_log = RunLog.create(tmp_path / "runs", "sample")
        self.meter = FilmMeter(Decimal("2.00"), self.run_log)

    async def write(self, steps: list[Step]) -> tuple[Any, Any]:
        client = FakeClient(steps)
        posts = await write_posts("sample", MOMENTS, servers=self.servers, client=client, meter=self.meter)
        return posts, client.messages

    def events(self, kind: str) -> list[dict[str, Any]]:
        lines = [json.loads(line) for line in self.run_log.path.read_text().splitlines()]
        return [line for line in lines if line["event"] == kind]


async def test_the_copywriter_returns_six_traced_posts(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    posts, fake = await run.write([read_catalog, submit()])

    assert [(p.platform, p.language) for p in posts] == [
        (p, lang) for p in ("facebook", "instagram", "tiktok") for lang in ("en", "vi")
    ]
    calls = run.events("model_call")
    assert len(calls) == len(fake.requests) == len(fake.counted) == 2
    assert all(c["model"] == MODEL and c["agent"] == "copywriter" for c in calls)
    for counted, sent in zip(fake.counted, fake.requests, strict=True):
        assert counted == {k: v for k, v in sent.items() if k != "max_tokens"}

    task = fake.requests[0]["messages"][0]["content"]
    assert "Film: sample" in task
    assert '"key_moment": 2' in task and "Just a trim, please." in task  # the Key Moments, as numbered data


async def test_the_copywriter_is_offered_only_the_catalog_and_never_the_spoiler_list(
    films: Path, tmp_path: Path
) -> None:
    run = Run(films, tmp_path)
    _, fake = await run.write([read_catalog, submit()])
    for request in fake.requests:
        assert [t["name"] for t in request["tools"]] == ["get_catalog", "submit_social_posts"]
        assert SPOILER not in json.dumps(request, default=str)


async def test_the_real_server_set_is_the_catalog_only(films: Path) -> None:
    names = set()
    for server in copywriter_servers(Settings(films_dir=films)):
        async with Client(server) as client:
            names |= {t.name for t in (await client.list_tools()).tools}
    assert names == {"get_catalog"}


async def test_an_untraced_submission_is_sent_back_with_its_problems(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    too_long = submit(1, text=EN + " " + "x" * 200)
    posts, fake = await run.write([read_catalog, too_long, submit(2)])

    [rejected] = results(fake.requests[2])
    assert rejected["is_error"]
    assert "tiktok" in rejected["content"][0]["text"] and "150" in rejected["content"][0]["text"]
    assert len(posts) == 6
    [event] = run.events("social_posts_rejected")
    assert event["attempt"] == 1


async def test_the_copywriter_gives_up_after_too_many_untraced_submissions(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    bad = [submit(i, text="no title, no Vietnamese") for i in range(MAX_SUBMISSIONS)]
    with pytest.raises(AgentFailed, match="traced"):
        await run.write([read_catalog, *bad])
    assert len(run.events("social_posts_rejected")) == MAX_SUBMISSIONS


async def test_submitting_before_reading_the_catalog_is_refused_and_not_counted(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    posts, fake = await run.write([submit(0), read_catalog, submit()])
    [refused] = results(fake.requests[1])
    assert refused["is_error"] and "get_catalog" in refused["content"][0]["text"]
    assert run.events("social_posts_rejected") == []
    assert len(posts) == 6


async def test_the_catalog_of_another_film_is_refused(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    other = lambda params: reply(use("get_catalog", film="another-film"))  # noqa: E731
    _, fake = await run.write([other, read_catalog, submit()])
    [refused] = results(fake.requests[1])
    assert refused["is_error"] and "sample" in refused["content"][0]["text"]
