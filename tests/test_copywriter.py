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


async def test_the_copywriter_gives_up_after_too_many_submissions_that_add_nothing(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    # T21: the first keeps five posts, so it uses no try. Only the ones that add nothing count.
    bad = [submit(i, text="no title, no Vietnamese") for i in range(MAX_SUBMISSIONS + 1)]
    with pytest.raises(AgentFailed, match="traced"):
        await run.write([read_catalog, *bad])
    assert len(run.events("social_posts_rejected")) == MAX_SUBMISSIONS + 1


async def test_submissions_with_nothing_usable_use_every_try(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    empty = [
        (lambda n: lambda params: reply(use("submit_social_posts", n, posts=[])))(i) for i in range(MAX_SUBMISSIONS)
    ]
    with pytest.raises(AgentFailed, match="traced"):
        await run.write([read_catalog, *empty])
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


# T6: revise mode rewrites only the posts the Checker rejected.


async def test_revise_rewrites_only_the_rejected_posts(films: Path, tmp_path: Path) -> None:
    from trailer_to_campaign.agents.copywriter import Rewrite, revise_posts
    from trailer_to_campaign.agents.social_posts import Claim, SocialPost

    rejected = SocialPost(
        "tiktok", "en", f'{TITLE}: "Just a trim, please." Stream it now.', [Claim("t", "catalog:title")]
    )
    reason = "The words 'Just a trim' reveal something the Film's team keeps secret. Remove them."
    fixed = {
        "platform": "tiktok",
        "language": "en",
        "text": f"{TITLE}: a spy walks into a barbershop. Stream it now.",
        "claims": [{"text": "The title", "source": "catalog:title"}],
    }
    # T21: a post nobody asked for is ignored, so only the post asked for has to be right.
    untraced_and_others = lambda params: reply(  # noqa: E731
        use("submit_social_posts", 1, posts=six_posts(text="no title here"))
    )
    just_one = lambda params: reply(use("submit_social_posts", 2, posts=[fixed]))  # noqa: E731

    run = Run(films, tmp_path)
    client = FakeClient([read_catalog, untraced_and_others, just_one])
    posts = await revise_posts(
        "sample", MOMENTS, [Rewrite(rejected, [reason])], servers=run.servers, client=client, meter=run.meter
    )

    assert [(p.platform, p.language, p.text) for p in posts] == [("tiktok", "en", fixed["text"])]
    [refused] = results(client.messages.requests[2])
    text = refused["content"][0]["text"]
    assert refused["is_error"] and text.splitlines()[2].endswith(": tiktok en")  # T18: it names what's asked for
    assert "Tries left: 2." in text
    task = client.messages.requests[0]["messages"][0]["content"]
    assert reason in task and json.dumps(rejected.text)[1:-1] in task and '"key_moment": 2' in task
    for request in client.messages.requests:
        assert SPOILER not in json.dumps(request, default=str)


# T18: a rejected first draft is told to send all six posts again, by name, and the Run Log keeps what it sent.


async def test_a_rejected_first_draft_is_told_what_was_kept_and_what_to_send(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    one_problem = submit(1, text=EN + " " + "x" * 200)
    posts, fake = await run.write([read_catalog, one_problem, submit(2)])

    [rejected] = results(fake.requests[2])
    lines = rejected["content"][0]["text"].splitlines()
    assert lines[1].endswith(": facebook en, facebook vi, instagram en, instagram vi, tiktok vi")
    assert "one submit_social_posts call" in lines[2] and lines[2].endswith(": tiktok en")
    # It kept five, so it used no try. "(2 left)" after a list could read as two posts left; this says tries.
    assert f"Tries left: {MAX_SUBMISSIONS}." in lines[0] and " left)" not in lines[0]
    assert len(posts) == 6


async def test_a_rejected_submission_is_kept_in_the_run_log(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    partial = lambda params: reply(use("submit_social_posts", 1, posts=six_posts()[:2]))  # noqa: E731
    await run.write([read_catalog, partial, submit(2)])

    [event] = run.events("social_posts_rejected")
    assert event["submitted"] == {"posts": six_posts()[:2]}  # as sent, so a partial set can be read back
    assert any("write one" in p for p in event["problems"])
    assert event["kept"] == ["facebook en", "facebook vi"] and event["ignored"] == []  # T21


async def test_a_malformed_submission_is_logged_as_sent(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    wrong_key = lambda params: reply(use("submit_social_posts", 1, poems=six_posts()[:1]))  # noqa: E731
    await run.write([read_catalog, wrong_key, submit(2)])

    [event] = run.events("social_posts_rejected")
    assert event["submitted"] == {"poems": six_posts()[:1]}  # not an empty list: what it sent is the clue


# T21: a post that passed tracing is kept, and a rejection asks only for what's still needed.


def sends(n: int, posts: list[dict[str, Any]]) -> Any:
    return lambda params: reply(use("submit_social_posts", n, posts=posts))


def the(platform: str, language: str, **over: Any) -> dict[str, Any]:
    [found] = [p for p in six_posts() if p["platform"] == platform and p["language"] == language]
    return {**found, **over}


TOO_LONG = EN + " " + "x" * 200  # over TikTok's limit
ALL_SIX = [
    ("facebook", "en"),
    ("facebook", "vi"),
    ("instagram", "en"),
    ("instagram", "vi"),
    ("tiktok", "en"),
    ("tiktok", "vi"),
]


def pairs(posts: list[Any]) -> list[tuple[str, str]]:
    return [(p.platform, p.language) for p in posts]


async def test_springs_shape_a_problem_in_two_then_only_those_two(films: Path, tmp_path: Path) -> None:
    """Spring: 6 posts with a problem in 2, and a retry that left one out. Now 2 is all it needs to send."""
    run = Run(films, tmp_path)
    first = six_posts()
    first[1] = {**first[1], "text": "no title here"}  # facebook vi
    first[4] = {**first[4], "text": TOO_LONG}  # tiktok en
    posts, _ = await run.write([read_catalog, sends(1, first), sends(2, [the("facebook", "vi"), the("tiktok", "en")])])

    assert pairs(posts) == ALL_SIX
    [event] = run.events("social_posts_rejected")
    assert event["kept"] == ["facebook en", "instagram en", "instagram vi", "tiktok vi"]


async def test_sprite_frights_shape_a_garbled_retry_keeps_the_five(films: Path, tmp_path: Path) -> None:
    """Sprite Fright: 6 posts with a problem in 1, then only a garbled copy of a post that was fine."""
    run = Run(films, tmp_path)
    first = six_posts(text=TOO_LONG)  # tiktok en
    garbled = [the("facebook", "en", text="y\ny", claims=[])]
    posts, fake = await run.write([read_catalog, sends(1, first), sends(2, garbled), sends(3, [the("tiktok", "en")])])

    assert pairs(posts) == ALL_SIX
    assert posts[0].text == EN and posts[0].claims  # the good copy stays: a kept post is never replaced
    first_no, second_no = [results(fake.requests[n])[0]["content"][0]["text"] for n in (2, 3)]
    assert "Tries left: 3." in first_no  # it kept five
    assert "Tries left: 2." in second_no  # it added nothing
    assert second_no.splitlines()[2].endswith(": tiktok en")  # the next try asks only for the 1
    events = run.events("social_posts_rejected")
    assert events[1]["ignored"] == ["facebook en"] and len(events[1]["kept"]) == 5


async def test_t10s_shape_a_good_post_left_out_of_a_retry_is_not_lost(films: Path, tmp_path: Path) -> None:
    """T10: try 1 had one problem, and tries 2 and 3 sent 2 posts. The Run ended with no posts."""
    run = Run(films, tmp_path)
    first = six_posts(text=TOO_LONG)
    two = [the("facebook", "en"), the("instagram", "en", text="no title, no claims", claims=[])]
    posts, _ = await run.write(
        [read_catalog, sends(1, first), sends(2, two), sends(3, two), sends(4, [the("tiktok", "en")])]
    )

    assert pairs(posts) == ALL_SIX  # the same two tries used to end the Run; now one try is still left


async def test_a_kept_post_sent_beside_the_needed_one_is_ignored_and_the_set_accepted(
    films: Path, tmp_path: Path
) -> None:
    run = Run(films, tmp_path)
    changed = the("facebook", "en", text=f"{TITLE}: another version. Watch it now.")
    posts, _ = await run.write([read_catalog, submit(1, text=TOO_LONG), sends(2, [changed, the("tiktok", "en")])])

    assert pairs(posts) == ALL_SIX and posts[0].text == EN
    [ignored] = run.events("social_posts_ignored")
    assert ignored["posts"] == ["facebook en"]


async def test_only_posts_already_kept_adds_nothing_and_uses_a_try(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    kept_only = [the("facebook", "en")]
    with pytest.raises(AgentFailed, match="traced"):
        await run.write(
            [read_catalog, submit(1, text=TOO_LONG), *[sends(n, kept_only) for n in range(2, MAX_SUBMISSIONS + 2)]]
        )


async def test_every_post_returned_passed_tracing(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    still_bad = sends(2, [the("tiktok", "en", text="no title here")])
    posts, _ = await run.write([read_catalog, submit(1, text=TOO_LONG), still_bad, sends(3, [the("tiktok", "en")])])

    assert all("no title" not in p.text and p.claims for p in posts)
    assert [p.text for p in posts if (p.platform, p.language) == ("tiktok", "en")] == [EN]


async def test_a_rewrite_keeps_its_traced_posts_and_returns_only_what_was_asked(films: Path, tmp_path: Path) -> None:
    from trailer_to_campaign.agents.copywriter import Rewrite, revise_posts
    from trailer_to_campaign.agents.social_posts import Claim, SocialPost

    run = Run(films, tmp_path)
    claims = [Claim("He asks for a trim", "key_moment:2")]
    asked = [
        Rewrite(SocialPost("tiktok", "en", TOO_LONG, claims), ["Too much."]),
        Rewrite(SocialPost("facebook", "vi", VI, claims), ["Too much."]),
    ]
    rewritten_en = f'{TITLE}: "Just a trim, please." A new take. Watch it now.'
    first = [
        the("tiktok", "en", text=rewritten_en),
        the("facebook", "vi", text="no title here"),
        the("instagram", "en"),
    ]
    client = FakeClient([read_catalog, sends(1, first), sends(2, [the("facebook", "vi")])])
    posts = await revise_posts("sample", MOMENTS, asked, servers=run.servers, client=client, meter=run.meter)

    assert pairs(posts) == [("facebook", "vi"), ("tiktok", "en")]
    assert posts[1].text == rewritten_en  # kept from the first try
    [event] = run.events("social_posts_rejected")
    assert event["kept"] == ["tiktok en"] and event["ignored"] == ["instagram en"]


async def test_the_spoiler_list_stays_out_of_every_retry(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    _, fake = await run.write([read_catalog, submit(1, text=TOO_LONG), sends(2, [the("tiktok", "en")])])
    for request in fake.requests:
        assert SPOILER not in json.dumps(request, default=str)
