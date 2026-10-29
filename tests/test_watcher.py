"""Tests for the Watcher's hand-written loop, against the real footage and catalog MCP servers.

Only the Claude API is faked: a scripted client that answers each turn and
records every request, so the tests see exactly what the model would see.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from anthropic.types import Message
from clips import make_clip

from trailer_to_campaign.agents.loop import AgentFailed
from trailer_to_campaign.agents.watcher import MAX_SUBMISSIONS, MAX_TURNS, MODEL, watch_trailer, watcher_servers
from trailer_to_campaign.budget import FilmBudgetReached, FilmMeter
from trailer_to_campaign.config import Settings
from trailer_to_campaign.footage.transcript import Segment, Transcript
from trailer_to_campaign.run_log import RunLog
from trailer_to_campaign.tools.catalog import create_catalog_server
from trailer_to_campaign.tools.footage import create_server

pytestmark = pytest.mark.anyio

FILM_TOML = """
[catalog]
title = "Sample"
synopsis = "A sample Film."
genre = ["drama"]
language = "en"
runtime_minutes = 90

[[catalog.cast]]
character = "Agent 327"

[[catalog.cast]]
character = "Boris Kloris"

[[spoilers]]
id = "ending"
text = "The secret procedure happens at the end."
"""
SPOILER = "secret procedure"
COUNTED_TOKENS = 3_000


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
def clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_clip(tmp_path_factory.mktemp("clip") / "trailer.mp4")


@pytest.fixture
def films(tmp_path: Path, clip: Path) -> Path:
    film = tmp_path / "films" / "sample"
    (film / "source").mkdir(parents=True)
    shutil.copy(clip, film / "source" / "trailer.mp4")
    (film / "film.toml").write_text(FILM_TOML, encoding="utf-8")
    return tmp_path / "films"


class Transcriber:
    name = "fake:small"

    def __call__(self, video: Path) -> Transcript:
        return Transcript("en", [Segment(1.0, 2.5, "The river remembers.")], unheard=[Segment(9.0, 9.5, "You")])


class Downloads:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, url: str, dest: Path) -> Path:
        self.urls.append(url)
        raise AssertionError("no test should download")


Step = Callable[[dict[str, Any]], Message]


class FakeMessages:
    """Answers each create() with the next scripted step, and records every request."""

    def __init__(self, steps: list[Step]) -> None:
        self.steps = list(steps)
        self.requests: list[dict[str, Any]] = []
        self.counted: list[dict[str, Any]] = []

    async def count_tokens(self, **params: Any) -> Any:
        self.counted.append({**params, "messages": list(params["messages"])})
        return type("Count", (), {"input_tokens": COUNTED_TOKENS})()

    async def create(self, **params: Any) -> Message:
        self.requests.append({**params, "messages": list(params["messages"])})
        if not self.steps:
            raise AssertionError("the loop asked for more turns than scripted")
        return self.steps.pop(0)(params)


class FakeClient:
    def __init__(self, steps: list[Step]) -> None:
        self.messages = FakeMessages(steps)


def reply(*content: dict[str, Any], stop: str = "tool_use") -> Message:
    return Message.model_validate(
        {
            "id": "msg_x",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": list(content),
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 2_000, "output_tokens": 300},
        }
    )


def use(name: str, n: int = 1, **arguments: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"tu_{name}_{n}", "name": name, "input": arguments}


THINKING = {"type": "thinking", "thinking": "", "signature": "sig-1"}


def results(params: dict[str, Any]) -> list[dict[str, Any]]:
    """The tool_result blocks the loop sent back in this request."""
    return params["messages"][-1]["content"]


def result_json(params: dict[str, Any], tool_use_id: str) -> Any:
    [block] = [b for b in results(params) if b["tool_use_id"] == tool_use_id]
    assert not block["is_error"], block["content"]
    return json.loads(block["content"][0]["text"])


def footage_of(params: dict[str, Any]) -> dict[str, Any]:
    for message in reversed(params["messages"]):
        if message["role"] == "user" and isinstance(message["content"], list):
            for block in message["content"]:
                if block.get("tool_use_id") == "tu_watch_footage_1" and not block["is_error"]:
                    return json.loads(block["content"][0]["text"])
    raise AssertionError("no watch_footage result yet")


def grounded_moments(footage: dict[str, Any]) -> list[dict[str, Any]]:
    moments = [
        {
            "start": f["t"],
            "end": min(f["t"] + 1.0, footage["duration"]),
            "what_happens": "The screen changes colour.",
            "mood": "calm",
            "quote": None,
            "characters": [],
            "frames": [f["index"]],
        }
        for f in footage["frames"]
    ]
    moments[0] = {**moments[0], "quote": "the river remembers", "characters": ["agent 327"]}
    while len(moments) < 5:
        moments.append(moments[-1])
    return moments[:10]


def look(params: dict[str, Any]) -> Message:
    """Catalog and footage first, as the prompt asks."""
    return reply(
        THINKING,
        use("get_catalog", film="sample"),
        use("watch_footage", film="sample", source="trailer.mp4", kind="trailer"),
    )


def see_every_frame(params: dict[str, Any]) -> Message:
    footage = footage_of(params)
    return reply(
        *[
            use("get_frame", f["index"], film="sample", footage_id=footage["footage_id"], index=f["index"])
            for f in footage["frames"]
        ]
    )


def submit(make: Callable[[dict[str, Any]], list[dict[str, Any]]] = grounded_moments, n: int = 1) -> Step:
    return lambda params: reply(use("submit_key_moments", n, key_moments=make(footage_of(params))))


class Run:
    def __init__(self, films: Path, tmp_path: Path, budget: str = "2.00") -> None:
        self.downloads = Downloads()
        self.servers = [
            create_server(films_dir=films, downloader=self.downloads, transcriber=Transcriber()),
            create_catalog_server(films),
        ]
        self.run_log = RunLog.create(tmp_path / "runs", "sample")
        self.meter = FilmMeter(Decimal(budget), self.run_log)

    async def watch(self, steps: list[Step], source: str = "trailer.mp4") -> tuple[Any, FakeMessages]:
        client = FakeClient(steps)
        try:
            moments = await watch_trailer("sample", source, servers=self.servers, client=client, meter=self.meter)
        except BaseException as err:
            err.fake = client.messages  # type: ignore[attr-defined]
            raise
        return moments, client.messages

    def events(self, kind: str) -> list[dict[str, Any]]:
        lines = [json.loads(line) for line in self.run_log.path.read_text().splitlines()]
        return [line for line in lines if line["event"] == kind]


async def test_the_watcher_returns_grounded_key_moments(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    moments, fake = await run.watch([look, see_every_frame, submit()])

    assert 5 <= len(moments) <= 10
    assert moments[0].quote is not None and moments[0].quote.text == "the river remembers"
    assert moments[0].characters == ["Agent 327"]
    assert [m.start for m in moments] == sorted(m.start for m in moments)

    # Every request went through the meter, reserved from the counted prompt.
    calls = run.events("model_call")
    assert len(calls) == len(fake.requests) == 3
    assert all(c["model"] == MODEL and c["agent"] == "watcher" and c["charged"] == "actual" for c in calls)
    assert len(fake.counted) == 3
    for counted, sent in zip(fake.counted, fake.requests, strict=True):
        assert counted == {k: v for k, v in sent.items() if k != "max_tokens"}  # the whole prompt, as sent
    assert fake.requests[0]["output_config"] == {"effort": "medium"}
    assert fake.requests[0]["cache_control"] == {"type": "ephemeral"}
    assert run.downloads.urls == []


async def test_frames_reach_the_model_as_images_and_history_is_resent_unchanged(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    _, fake = await run.watch([look, see_every_frame, submit()])

    first_reply = fake.requests[1]["messages"][1]
    assert first_reply["role"] == "assistant"
    assert first_reply["content"][0].type == "thinking" and first_reply["content"][0].signature == "sig-1"
    image = results(fake.requests[2])[0]["content"][0]
    assert image["type"] == "image"
    assert image["source"]["type"] == "base64" and image["source"]["media_type"] == "image/jpeg"
    assert len(image["source"]["data"]) > 100
    # Several tool calls in one turn come back together, in one user message.
    assert len(results(fake.requests[2])) == len(footage_of(fake.requests[2])["frames"])


async def test_an_ungrounded_submission_is_sent_back_with_its_problems(films: Path, tmp_path: Path) -> None:
    def invented(footage: dict[str, Any]) -> list[dict[str, Any]]:
        moments = grounded_moments(footage)
        moments[1] = {**moments[1], "characters": ["Dr. Nobody"], "quote": "Thanks for watching"}
        return moments

    run = Run(films, tmp_path)
    moments, fake = await run.watch([look, see_every_frame, submit(invented), submit(n=2)])

    [rejected] = results(fake.requests[3])
    assert rejected["is_error"]
    text = rejected["content"][0]["text"]
    assert "Dr. Nobody" in text and "Thanks for watching" in text
    assert len(moments) >= 5
    [event] = run.events("key_moments_rejected")
    assert any("Dr. Nobody" in p for p in event["problems"])


async def test_submitting_before_watching_is_refused(films: Path, tmp_path: Path) -> None:
    early = lambda params: reply(use("submit_key_moments", key_moments=[]))  # noqa: E731
    run = Run(films, tmp_path)
    await run.watch([early, look, see_every_frame, submit(n=2)])
    assert run.events("key_moments_rejected") == []  # not a grounding attempt, so it doesn't count


async def test_the_watcher_gives_up_after_too_many_ungrounded_submissions(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    bad = [submit(lambda f: grounded_moments(f)[:3], n=i) for i in range(MAX_SUBMISSIONS)]
    with pytest.raises(AgentFailed, match="grounded"):
        await run.watch([look, see_every_frame, *bad])
    assert len(run.events("key_moments_rejected")) == MAX_SUBMISSIONS


async def test_the_watcher_gives_up_after_too_many_turns(films: Path, tmp_path: Path) -> None:
    chat = lambda params: reply({"type": "text", "text": "Still thinking."}, stop="end_turn")  # noqa: E731
    run = Run(films, tmp_path)
    with pytest.raises(AgentFailed, match=str(MAX_TURNS)) as caught:
        await run.watch([chat] * MAX_TURNS)
    fake = caught.value.fake  # type: ignore[attr-defined]
    nudge = fake.requests[1]["messages"][-1]
    assert nudge["role"] == "user" and "submit_key_moments" in nudge["content"]


@pytest.mark.parametrize("stop", ["max_tokens", "refusal"])
async def test_a_cut_off_or_refused_reply_ends_the_run(films: Path, tmp_path: Path, stop: str) -> None:
    run = Run(films, tmp_path)
    with pytest.raises(AgentFailed, match=stop):
        await run.watch([lambda params: reply({"type": "text", "text": "..."}, stop=stop)])
    assert len(run.events("model_call")) == 1  # it was still paid for


@pytest.mark.parametrize(
    "call",
    [
        use("get_catalog", film="another-film"),
        use("watch_footage", film="sample", source="https://www.youtube.com/watch?v=other", kind="trailer"),
        use("watch_footage", film="another-film", source="trailer.mp4", kind="trailer"),
        use("get_frame", film="sample", footage_id="0123456789ab", index=0),
        use("get_spoiler_list", film="sample"),
    ],
)
async def test_a_call_outside_this_runs_film_and_trailer_never_reaches_a_server(
    films: Path, tmp_path: Path, call: dict[str, Any]
) -> None:
    run = Run(films, tmp_path)
    off = lambda params: reply(call)  # noqa: E731
    _, fake = await run.watch([off, look, see_every_frame, submit()])
    [refused] = results(fake.requests[1])
    assert refused["is_error"]
    assert run.downloads.urls == []
    assert not (films / "another-film").exists()


async def test_the_watcher_is_offered_only_its_tools_and_never_the_spoiler_list(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path)
    _, fake = await run.watch([look, see_every_frame, submit()])
    for request in fake.requests:
        assert [t["name"] for t in request["tools"]] == [
            "get_catalog",
            "get_frame",
            "submit_key_moments",
            "watch_footage",
        ]
        assert SPOILER not in json.dumps(request, default=str)


async def test_the_real_server_set_is_footage_and_catalog_only(films: Path) -> None:
    from mcp import Client

    names = set()
    for server in watcher_servers(Settings(films_dir=films)):
        async with Client(server) as client:
            names |= {t.name for t in (await client.list_tools()).tools}
    assert names == {"get_catalog", "get_frame", "watch_footage"}


async def test_a_film_budget_too_small_for_the_first_call_sends_nothing(films: Path, tmp_path: Path) -> None:
    run = Run(films, tmp_path, budget="0.01")
    with pytest.raises(FilmBudgetReached) as caught:
        await run.watch([look])
    assert caught.value.fake.requests == []  # type: ignore[attr-defined]
    assert run.meter.spent == 0
