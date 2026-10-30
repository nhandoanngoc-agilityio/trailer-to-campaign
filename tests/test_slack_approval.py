"""Tests for Approval in Slack: each draft gets Approve / Reject buttons, and only a Marketer's click decides."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from trailer_to_campaign.agents.key_moments import KeyMoment, Quote
from trailer_to_campaign.agents.review import CheckedPost
from trailer_to_campaign.agents.social_posts import SocialPost
from trailer_to_campaign.approval import ApprovalAborted, KeyMomentDecision, PostDecision
from trailer_to_campaign.slack_approval import SlackApprover

MARKETER, PASSER_BY = "U07MARKET", "U07OTHER"
MOMENT = KeyMoment(
    84.0, 90.0, "A cape is drawn up to his chin.", "deadpan", Quote("Just a trim, please.", 86, 88), ["Agent 327"], [34]
)
POST = CheckedPost(SocialPost("tiktok", "en", '"Just a trim, please." Stream it now.', []), "passed", [], 0)
FLAGGED = CheckedPost(
    SocialPost("facebook", "en", '"Doctor, carry on with the procedure." Stream it now.', []),
    "flagged",
    ["spoiler ending-capture: gives away the ending"],
    2,
)


class FakeClient:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.ephemeral: list[dict[str, Any]] = []

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:  # noqa: N802
        self.posted.append(kwargs)
        return {"ok": True, "ts": f"100.{len(self.posted)}"}

    def chat_update(self, **kwargs: Any) -> dict[str, Any]:  # noqa: N802
        self.updated.append(kwargs)
        return {"ok": True}

    def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]:  # noqa: N802
        self.ephemeral.append(kwargs)
        return {"ok": True}


def approver() -> tuple[SlackApprover, FakeClient]:
    client = FakeClient()
    return SlackApprover(client, "C0TEST", "1.0", frozenset({MARKETER}), run_id="run1"), client


def buttons(message: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for b in message["blocks"] if b["type"] == "actions" for e in b["elements"]]


def text_of(message: dict[str, Any]) -> str:
    return " ".join(str(b) for b in message["blocks"])


def no_recheck(text: str) -> Any:
    raise AssertionError("T9 has no Edit, so no re-check")


@pytest.mark.parametrize(("click", "decision"), [("t2c_approve", "approve"), ("t2c_reject", "reject")])
def test_a_marketers_click_decides_a_key_moment(click: str, decision: str) -> None:
    slack, client = approver()
    slack.click(click, "run1:km1", MARKETER)  # waits in the queue until the draft is asked about
    assert slack.key_moment(1, 5, MOMENT) == KeyMomentDecision(decision, by=MARKETER)

    [posted] = client.posted
    assert posted["channel"] == "C0TEST" and posted["thread_ts"] == "1.0"
    assert "A cape is drawn up" in text_of(posted) and "Just a trim, please." in text_of(posted)
    assert [(b["action_id"], b["value"]) for b in buttons(posted)] == [
        ("t2c_approve", "run1:km1"),
        ("t2c_reject", "run1:km1"),
    ]
    [updated] = client.updated  # the decision replaces the buttons, so it can't be clicked twice
    assert updated["ts"] == "100.1" and buttons(updated) == []
    assert f"<@{MARKETER}>" in text_of(updated) and decision.capitalize() in text_of(updated)


def test_a_click_from_someone_who_is_not_a_marketer_decides_nothing() -> None:
    slack, client = approver()
    slack.click("t2c_approve", "run1:km1", PASSER_BY)
    slack.click("t2c_reject", "run1:km1", MARKETER)
    assert slack.key_moment(1, 5, MOMENT) == KeyMomentDecision("reject", by=MARKETER)
    [refused] = client.ephemeral
    assert refused["user"] == PASSER_BY and "Marketer" in refused["text"]


@pytest.mark.parametrize("value", ["run1:km9", "old-run:km1", "garbage"])
def test_a_click_on_another_draft_or_run_decides_nothing(value: str) -> None:
    slack, client = approver()
    slack.click("t2c_approve", value, MARKETER)
    slack.click("t2c_reject", "run1:km1", MARKETER)
    assert slack.key_moment(1, 5, MOMENT).decision == "reject"
    [told] = client.ephemeral
    assert told["user"] == MARKETER and "already" in told["text"]


def test_the_approver_waits_for_a_click() -> None:
    slack, _ = approver()
    later = threading.Timer(0.05, slack.click, args=("t2c_approve", "run1:km1", MARKETER))
    later.start()
    started = time.monotonic()
    assert slack.key_moment(1, 5, MOMENT).decision == "approve"
    assert time.monotonic() - started >= 0.04


def test_stopping_the_app_never_approves() -> None:
    slack, client = approver()
    threading.Timer(0.05, slack.close).start()
    with pytest.raises(ApprovalAborted):
        slack.key_moment(1, 5, MOMENT)
    with pytest.raises(ApprovalAborted):  # and nothing after it is asked
        slack.social_post(1, 6, POST, no_recheck)


def test_a_post_is_decided_and_a_flagged_one_shows_its_problems() -> None:
    slack, client = approver()
    slack.click("t2c_approve", "run1:post1", MARKETER)
    slack.click("t2c_reject", "run1:post2", MARKETER)
    assert slack.social_post(1, 6, POST, no_recheck) == PostDecision("approve", by=MARKETER)
    assert slack.social_post(2, 6, FLAGGED, no_recheck) == PostDecision("reject", by=MARKETER)
    assert "FLAGGED" in text_of(client.posted[1]) and "gives away the ending" in text_of(client.posted[1])


def test_model_text_cannot_ping_or_link_in_slack() -> None:
    sneaky = KeyMoment(
        1.0, 2.0, "A sign reads <!channel> and <@U0BOSS> and <https://evil.example|click>.", "odd", None, [], [1]
    )
    slack, client = approver()
    slack.click("t2c_reject", "run1:km1", MARKETER)
    slack.key_moment(1, 1, sneaky)
    shown = text_of(client.posted[0])
    assert "<!channel>" not in shown and "<@U0BOSS>" not in shown and "<https://evil" not in shown
    assert "&lt;!channel&gt;" in shown
