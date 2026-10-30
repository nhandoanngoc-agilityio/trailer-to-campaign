"""Tests for the Run Thread as the pipeline's output: what a Run says becomes messages in its thread."""

from __future__ import annotations

from typing import Any

from trailer_to_campaign.slack_thread import SlackThread


class FakeClient:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:  # noqa: N802 (Slack's method name)
        self.posted.append(kwargs)
        return {"ok": True}


def test_each_thing_the_run_says_is_a_message_in_its_thread() -> None:
    client = FakeClient()
    thread = SlackThread(client, "C0TEST", "1793328548.622579")
    print("\nThe Watcher is studying the trailer...", file=thread, flush=True)
    print("Run completed: spent $0.36 of $2.00.\nApproval Rate 15/16.", file=thread, flush=True)
    assert [p["text"] for p in client.posted] == [
        "The Watcher is studying the trailer...",
        "Run completed: spent $0.36 of $2.00.\nApproval Rate 15/16.",
    ]
    assert all(p["channel"] == "C0TEST" and p["thread_ts"] == "1793328548.622579" for p in client.posted)


def test_nothing_but_blank_lines_posts_nothing() -> None:
    client = FakeClient()
    thread = SlackThread(client, "C0TEST", "1.2")
    print("", file=thread, flush=True)
    thread.flush()
    assert client.posted == []


def test_what_is_written_without_a_flush_is_posted_at_the_next_flush() -> None:
    client = FakeClient()
    thread = SlackThread(client, "C0TEST", "1.2")
    print("Run for agent-327 with a Film Budget of $2.00", file=thread)
    assert client.posted == []
    thread.flush()
    assert [p["text"] for p in client.posted] == ["Run for agent-327 with a Film Budget of $2.00"]


# From /code-review: progress in Slack is best-effort, plain text, and short enough to post.


class BrokenClient:
    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:  # noqa: N802
        raise RuntimeError("ratelimited")


def test_a_slack_failure_never_reaches_the_run() -> None:
    thread = SlackThread(BrokenClient(), "C0TEST", "1.2")
    print("Run completed: spent $0.36 of $2.00.", file=thread, flush=True)  # must not raise


def test_what_the_run_says_is_shown_as_plain_text() -> None:
    client = FakeClient()
    thread = SlackThread(client, "C0TEST", "1.2")
    print('{"text": "A post with <!channel> and <@U0BOSS>"}', file=thread, flush=True)
    [posted] = client.posted
    assert "<!channel>" not in posted["text"] and "&lt;!channel&gt;" in posted["text"]


def test_a_very_long_message_is_cut_short_not_refused() -> None:
    client = FakeClient()
    thread = SlackThread(client, "C0TEST", "1.2")
    print("x" * 50_000, file=thread, flush=True)
    [posted] = client.posted
    assert len(posted["text"]) < 4_000 and "Run Log" in posted["text"]
