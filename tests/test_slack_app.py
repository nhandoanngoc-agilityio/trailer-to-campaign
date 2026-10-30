"""Tests for the Slack front door: a Marketer's @mention starts one Run, in its own Run Thread."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from trailer_to_campaign.agents.key_moments import KeyMoment
from trailer_to_campaign.approval import ApprovalAborted
from trailer_to_campaign.slack_app import FrontDoor
from trailer_to_campaign.slack_approval import SlackApprover
from trailer_to_campaign.slack_request import RunRequest
from trailer_to_campaign.slack_thread import SlackThread

MARKETER, PASSER_BY, BOT = "U07MARKET", "U07OTHER", "U0BOT"
TRAILER = "https://www.youtube.com/watch?v=mN0zPOpADL4"
MOMENT = KeyMoment(1.0, 2.0, "A spy walks in.", "cool", None, [], [1])


@pytest.fixture
def films(tmp_path: Path) -> Path:
    film = tmp_path / "films" / "agent-327"
    film.mkdir(parents=True)
    (film / "film.toml").write_text(
        '[catalog]\ntitle = "Agent 327"\nsynopsis = "A spy."\ngenre = ["comedy"]\nlanguage = "en"\n'
        f'runtime_minutes = 4\ntrailer = "{TRAILER}"\n\n[[catalog.cast]]\ncharacter = "Agent 327"\n\n'
        '[[spoilers]]\nid = "ending"\ntext = "He is caught."\n',
        encoding="utf-8",
    )
    return tmp_path / "films"


class FakeClient:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.reactions: list[dict[str, Any]] = []
        self.ephemeral: list[dict[str, Any]] = []

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:  # noqa: N802
        self.posted.append(kwargs)
        return {"ok": True, "ts": f"200.{len(self.posted)}"}

    def chat_update(self, **kwargs: Any) -> dict[str, Any]:  # noqa: N802
        return {"ok": True}

    def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]:  # noqa: N802
        self.ephemeral.append(kwargs)
        return {"ok": True}

    def reactions_add(self, **kwargs: Any) -> dict[str, Any]:
        self.reactions.append(kwargs)
        return {"ok": True}


class Runs:
    """Stands in for run_film: records each Run it is asked to start, and can play it later."""

    def __init__(self) -> None:
        self.started: list[tuple[RunRequest, SlackApprover, SlackThread]] = []
        self.pending: list[Callable[[], None]] = []
        self.body: Callable[[SlackApprover], Any] = lambda approver: 0

    def runner(self, request: RunRequest, approver: SlackApprover, thread: SlackThread) -> int:
        self.started.append((request, approver, thread))
        return self.body(approver)

    def start_later(self, work: Callable[[], None]) -> None:
        self.pending.append(work)


def door(films: Path, runs: Runs, start: Callable[[Callable[[], None]], None] | None = None) -> FrontDoor:
    return FrontDoor(
        films_dir=films,
        marketers=frozenset({MARKETER}),
        runner=runs.runner,
        start=start or (lambda work: work()),
    )


def mention(text: str, user: str = MARKETER, ts: str = "100.1", thread_ts: str | None = None) -> dict[str, Any]:
    event = {"type": "app_mention", "user": user, "text": f"<@{BOT}> {text}", "channel": "C0TEST", "ts": ts}
    if thread_ts:
        event["thread_ts"] = thread_ts
    return event


def click(value: str, user: str = MARKETER, action: str = "t2c_approve") -> dict[str, Any]:
    return {"user": {"id": user}, "channel": {"id": "C0TEST"}, "actions": [{"action_id": action, "value": value}]}


def test_a_marketers_mention_starts_a_run_in_a_thread_under_it(films: Path) -> None:
    runs, client = Runs(), FakeClient()
    door(films, runs).handle_mention(mention("agent-327"), client)

    [(request, approver, thread)] = runs.started
    assert request == RunRequest("agent-327", TRAILER)
    assert (thread.channel, thread.thread_ts) == ("C0TEST", "100.1")  # the Run Thread is the mention's thread
    assert (approver.channel, approver.thread_ts) == ("C0TEST", "100.1")
    assert client.reactions == [{"channel": "C0TEST", "timestamp": "100.1", "name": "eyes"}]


def test_someone_who_is_not_a_marketer_cannot_start_a_run(films: Path) -> None:
    runs, client = Runs(), FakeClient()
    door(films, runs).handle_mention(mention("agent-327", user=PASSER_BY), client)
    assert runs.started == []
    [reply] = client.posted
    assert reply["thread_ts"] == "100.1" and "Marketer" in reply["text"]


def test_a_request_that_cannot_run_gets_its_reason(films: Path) -> None:
    runs, client = Runs(), FakeClient()
    door(films, runs).handle_mention(mention("agent-237"), client)
    assert runs.started == []
    [reply] = client.posted
    assert "agent-327" in reply["text"]  # lists the Films it can run


def test_one_run_at_a_time(films: Path) -> None:
    runs, client = Runs(), FakeClient()
    front = door(films, runs, start=runs.start_later)
    front.handle_mention(mention("agent-327", ts="100.1"), client)
    front.handle_mention(mention("agent-327", ts="100.2"), client)
    assert len(runs.pending) == 1
    assert "in progress" in client.posted[-1]["text"] and client.posted[-1]["thread_ts"] == "100.2"

    runs.pending.pop()()  # the first Run ends...
    front.handle_mention(mention("agent-327", ts="100.3"), client)
    assert len(runs.pending) == 1  # ...and the next one can start


def test_a_mention_inside_an_old_thread_starts_a_new_run_thread(films: Path) -> None:
    runs, client = Runs(), FakeClient()
    door(films, runs).handle_mention(mention("agent-327", ts="100.9", thread_ts="100.1"), client)
    opened = client.posted[0]
    assert "thread_ts" not in opened and "agent-327" in opened["text"]  # a new top-level message...
    [(_, _, thread)] = runs.started
    assert thread.thread_ts == "200.1"  # ...is the new Run Thread
    pointer = client.posted[1]
    assert pointer["thread_ts"] == "100.1"  # and the old thread is told where the Run went


def test_a_click_reaches_the_runs_approver(films: Path) -> None:
    runs, client = Runs(), FakeClient()
    decided: list[str] = []
    runs.body = lambda approver: decided.append(approver.key_moment(1, 1, MOMENT).decision)
    front = door(films, runs, start=runs.start_later)
    front.handle_mention(mention("agent-327"), client)  # the Run's work is pending, its approver ready
    [run_id] = front.waiting_run_ids()

    front.handle_action(click(f"{run_id}:km1"), client)
    runs.pending.pop()()
    assert decided == ["approve"]


def test_a_click_for_a_run_that_is_not_waiting_is_answered_privately(films: Path) -> None:
    runs, client = Runs(), FakeClient()
    door(films, runs).handle_action(click("gone:km1"), client)
    [told] = client.ephemeral
    assert told["user"] == MARKETER and "no longer" in told["text"]


def test_stopping_the_app_ends_a_waiting_run_without_an_approval(films: Path) -> None:
    runs, client = Runs(), FakeClient()
    outcome: list[str] = []

    def body(approver: SlackApprover) -> int:
        try:
            approver.key_moment(1, 1, MOMENT)
        except ApprovalAborted:
            outcome.append("aborted")
        return 1

    runs.body = body
    front = door(films, runs, start=runs.start_later)
    front.handle_mention(mention("agent-327"), client)
    front.close()
    runs.pending.pop()()
    assert outcome == ["aborted"]


def test_a_run_that_crashes_says_so_and_frees_the_app(films: Path) -> None:
    runs, client = Runs(), FakeClient()

    def body(approver: SlackApprover) -> int:
        raise RuntimeError("boom")

    runs.body = body
    front = door(films, runs)
    front.handle_mention(mention("agent-327", ts="100.1"), client)
    assert any("stopped" in p["text"] and p.get("thread_ts") == "100.1" for p in client.posted)
    runs.body = lambda approver: 0
    front.handle_mention(mention("agent-327", ts="100.2"), client)
    assert len(runs.started) == 2


def test_the_app_is_freed_even_if_the_last_post_to_the_thread_fails(
    films: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trailer_to_campaign import slack_app

    class FailingThread(SlackThread):
        def flush(self) -> None:
            raise RuntimeError("ratelimited")

        def close(self) -> None:  # the stream's finalizer would flush again
            pass

    def like_a_thread(work: Any) -> None:  # an error in a worker thread never reaches handle_mention
        try:
            work()
        except RuntimeError:
            pass

    monkeypatch.setattr(slack_app, "SlackThread", FailingThread)
    runs, client = Runs(), FakeClient()
    front = door(films, runs, start=like_a_thread)
    front.handle_mention(mention("agent-327", ts="100.1"), client)
    front.handle_mention(mention("agent-327", ts="100.2"), client)
    assert len(runs.started) == 2  # not stuck "in progress"
    assert front.waiting_run_ids() == []
