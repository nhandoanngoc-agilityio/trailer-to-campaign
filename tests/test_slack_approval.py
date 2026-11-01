"""Tests for Approval in Slack: each draft gets Approve / Reject buttons, and only a Marketer's click decides."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
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
        self.views: list[dict[str, Any]] = []
        self.draft_posted = threading.Event()

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:  # noqa: N802
        self.posted.append(kwargs)
        self.draft_posted.set()
        return {"ok": True, "ts": f"100.{len(self.posted)}"}

    def views_open(self, **kwargs: Any) -> dict[str, Any]:
        self.views.append(kwargs)
        return {"ok": True}

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
        ("t2c_edit", "run1:km1"),
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


def test_a_flagged_posts_reasons_go_only_to_the_marketers() -> None:
    """The Checker's reasons can describe the spoiler: the channel sees that a post was flagged, not why."""
    client = FakeClient()
    slack = SlackApprover(client, "C0TEST", "1.0", frozenset({MARKETER, "U07SECOND"}), run_id="run1")
    slack.click("t2c_approve", "run1:post1", MARKETER)
    slack.click("t2c_reject", "run1:post2", MARKETER)
    assert slack.social_post(1, 6, POST, no_recheck) == PostDecision("approve", by=MARKETER)
    assert slack.social_post(2, 6, FLAGGED, no_recheck) == PostDecision("reject", by=MARKETER)

    in_channel = text_of(client.posted[1]) + " " + text_of(client.updated[1])
    assert "FLAGGED" in in_channel and "gives away the ending" not in in_channel
    assert "ending-capture" not in in_channel  # a Spoiler List id can say too much
    privately = {e["user"]: e["text"] for e in client.ephemeral}
    assert set(privately) == {MARKETER, "U07SECOND"}
    assert all("gives away the ending" in text for text in privately.values())


# From /code-review: a Slack hiccup never loses a decision, and a long draft still fits.


class UpdateFails(FakeClient):
    def chat_update(self, **kwargs: Any) -> dict[str, Any]:  # noqa: N802
        raise RuntimeError("message_not_found")


def test_a_decision_survives_a_failed_message_update() -> None:
    client = UpdateFails()
    slack = SlackApprover(client, "C0TEST", "1.0", frozenset({MARKETER}), run_id="run1")
    slack.click("t2c_approve", "run1:km1", MARKETER)
    assert slack.key_moment(1, 5, MOMENT) == KeyMomentDecision("approve", by=MARKETER)


def test_a_long_draft_is_split_into_blocks_slack_accepts() -> None:
    long_post = CheckedPost(SocialPost("instagram", "en", "&<> " * 700 + "Stream it now.", []), "passed", [], 0)
    slack, client = approver()
    slack.click("t2c_approve", "run1:post1", MARKETER)
    slack.social_post(1, 6, long_post, no_recheck)
    sections = [b for b in client.posted[0]["blocks"] if b["type"] == "section"]
    assert len(sections) > 1 and all(len(s["text"]["text"]) <= 3000 for s in sections)
    assert "Stream it now." in text_of(client.posted[0])


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


# T10: Edit opens a form; a saved edit is measured (and a post re-checked); then Approve as Light or heavy.

KM_EDIT = {"what_happens": "He grins as the cape goes up to his chin.", "mood": "sly"}
POST_EDIT = {"text": '"Just a trim, please." A haircut turns into a brawl. Stream it now.'}


def in_background(work: Callable[[], Any]) -> tuple[threading.Thread, list[Any]]:
    """Run a draft's Approval in its own thread, as a Run does, and keep what it returns."""
    result: list[Any] = []
    thread = threading.Thread(target=lambda: result.append(work()), daemon=True)  # a failing test can't hang the run
    thread.start()
    return thread, result


def form_values(view: dict[str, Any]) -> dict[str, str]:
    return {b["block_id"]: b["element"].get("initial_value", "") for b in view["blocks"] if b["type"] == "input"}


def test_edit_opens_a_form_filled_with_the_draft_for_a_marketer() -> None:
    slack, client = approver()
    thread, result = in_background(lambda: slack.key_moment(1, 5, MOMENT))
    assert client.draft_posted.wait(2)
    slack.open_editor(client, "trigger-1", "run1:km1", MARKETER)
    [opened] = client.views
    assert opened["trigger_id"] == "trigger-1"
    assert opened["view"]["private_metadata"] == "run1:km1" and opened["view"]["callback_id"] == "t2c_edit"
    assert form_values(opened["view"]) == {"what_happens": "A cape is drawn up to his chin.", "mood": "deadpan"}
    slack.click("t2c_reject", "run1:km1", MARKETER)
    thread.join(2)
    assert result == [KeyMomentDecision("reject", by=MARKETER)]


@pytest.mark.parametrize(
    ("user", "value", "says"), [(PASSER_BY, "run1:km1", "Marketer"), (MARKETER, "run1:km9", "already")]
)
def test_no_form_opens_for_someone_else_or_another_draft(user: str, value: str, says: str) -> None:
    slack, client = approver()
    thread, _ = in_background(lambda: slack.key_moment(1, 5, MOMENT))
    assert client.draft_posted.wait(2)
    slack.open_editor(client, "trigger-1", value, user)
    assert client.views == []
    assert says in client.ephemeral[-1]["text"] and client.ephemeral[-1]["user"] == user
    slack.click("t2c_reject", "run1:km1", MARKETER)
    thread.join(2)


def test_an_edited_key_moment_shows_its_measured_change_and_is_approved_as_a_light_edit() -> None:
    slack, client = approver()
    slack.submit_edit("run1:km1", MARKETER, KM_EDIT)
    slack.click("t2c_approve_light", "run1:km1:v1", MARKETER)  # the edited draft is version 1
    decision = slack.key_moment(1, 5, MOMENT)
    assert (decision.decision, decision.what_happens, decision.mood, decision.light, decision.by) == (
        "edit",
        KM_EDIT["what_happens"],
        "sly",
        True,
        MARKETER,
    )
    assert decision.edited_by == MARKETER and decision.changed is not None
    after_edit = client.updated[-2]
    assert f"{decision.changed:.0%} of the words" in text_of(after_edit)  # recorded = shown
    assert "He grins" in text_of(after_edit) and "% of the words" in text_of(after_edit)
    assert [b["action_id"] for b in buttons(after_edit)] == [
        "t2c_approve_light",
        "t2c_approve_heavy",
        "t2c_edit",
        "t2c_reject",
    ]
    final = client.updated[-1]
    assert buttons(final) == [] and "Light Edit" in text_of(final) and f"<@{MARKETER}>" in text_of(final)


def test_an_edited_post_is_rechecked_and_a_flag_stays_private() -> None:
    from trailer_to_campaign.agents.checker import Problem, Verdict
    from trailer_to_campaign.approval import Recheck

    rechecked: list[str] = []

    def recheck(text: str) -> Recheck:
        rechecked.append(text)
        spoiler = Problem(
            "spoiler", "brawl", "Hints at how the fight ends: he is captured.", spoiler_id="ending-capture"
        )
        return Recheck(["no claim cites the brawl"], Verdict(False, [spoiler]), note="Facts you added aren't traced")

    slack, client = approver()
    slack.submit_edit("run1:post1", MARKETER, POST_EDIT)
    slack.click("t2c_approve_heavy", "run1:post1:v1", MARKETER)
    decision = slack.social_post(1, 6, POST, recheck)
    assert (decision.decision, decision.text, decision.light, decision.by) == (
        "edit",
        POST_EDIT["text"],
        False,
        MARKETER,
    )
    assert rechecked == [POST_EDIT["text"]]
    in_channel = text_of(client.updated[-2])  # the draft as edited and re-checked
    assert "no claim cites the brawl" in in_channel and "Facts you added" in in_channel  # code's checks: shown
    assert "flagged" in in_channel and "captured" not in in_channel and "ending-capture" not in in_channel
    assert any("captured" in e["text"] for e in client.ephemeral if e["user"] == MARKETER)  # the reason: private


def test_edit_again_starts_from_the_latest_edit() -> None:
    slack, client = approver()
    slack.submit_edit("run1:km1", MARKETER, KM_EDIT)
    thread, result = in_background(lambda: slack.key_moment(1, 5, MOMENT))
    assert client.draft_posted.wait(2)
    for _ in range(100):  # the edit is applied by the Run's thread
        if client.updated:
            break
        threading.Event().wait(0.01)
    slack.open_editor(client, "trigger-2", "run1:km1:v1", MARKETER)
    assert form_values(client.views[0]["view"]) == KM_EDIT
    slack.click("t2c_reject", "run1:km1:v1", MARKETER)
    thread.join(2)
    assert result == [KeyMomentDecision("reject", by=MARKETER)]


def test_an_edit_that_changes_nothing_leaves_the_draft_as_it_was() -> None:
    slack, client = approver()
    slack.submit_edit("run1:km1", MARKETER, {"what_happens": "A cape is drawn up to his chin.", "mood": "deadpan"})
    slack.click("t2c_approve", "run1:km1", MARKETER)
    assert slack.key_moment(1, 5, MOMENT) == KeyMomentDecision("approve", by=MARKETER)
    assert any("nothing" in e["text"] for e in client.ephemeral)


def test_buttons_out_of_step_decide_nothing() -> None:
    slack, client = approver()
    slack.click("t2c_approve_light", "run1:km1", MARKETER)  # no edit yet
    slack.submit_edit("run1:km1", MARKETER, KM_EDIT)
    slack.click("t2c_approve", "run1:km1:v1", MARKETER)  # an edit is pending: Approve as Light or heavy
    slack.click("t2c_approve_heavy", "run1:km1:v1", MARKETER)
    decision = slack.key_moment(1, 5, MOMENT)
    assert decision.decision == "edit" and decision.light is False
    assert len(client.ephemeral) == 2


# From /code-review: an Approval is always for the text the Marketer saw.

SECOND = "U07SECOND"


def two_marketers() -> tuple[SlackApprover, FakeClient]:
    client = FakeClient()
    return SlackApprover(client, "C0TEST", "1.0", frozenset({MARKETER, SECOND}), run_id="run1"), client


def test_a_form_opened_on_older_text_never_overwrites_a_newer_edit() -> None:
    slack, client = two_marketers()
    slack.submit_edit("run1:km1", MARKETER, KM_EDIT)  # A's edit: the draft becomes version 1
    slack.submit_edit("run1:km1", SECOND, {"what_happens": "B's rewrite, from the old text.", "mood": "odd"})
    slack.click("t2c_approve_light", "run1:km1:v1", MARKETER)  # A approves the edit A saw
    decision = slack.key_moment(1, 5, MOMENT)
    assert decision.what_happens == KM_EDIT["what_happens"] and decision.by == MARKETER
    [told] = [e for e in client.ephemeral if e["user"] == SECOND]
    assert "changed since" in told["text"]


def test_a_click_on_an_older_version_of_the_draft_decides_nothing() -> None:
    slack, client = two_marketers()
    slack.submit_edit("run1:km1", MARKETER, KM_EDIT)
    slack.click("t2c_approve", "run1:km1", SECOND)  # a click on the message as it was before the edit
    slack.click("t2c_reject", "run1:km1:v1", MARKETER)
    assert slack.key_moment(1, 5, MOMENT).decision == "reject"
    assert any(e["user"] == SECOND for e in client.ephemeral)


def test_the_record_says_who_wrote_the_edit_and_who_approved_it() -> None:
    slack, _ = two_marketers()
    slack.submit_edit("run1:km1", SECOND, KM_EDIT)
    slack.click("t2c_approve_heavy", "run1:km1:v1", MARKETER)
    decision = slack.key_moment(1, 5, MOMENT)
    assert (decision.edited_by, decision.by) == (SECOND, MARKETER)


def test_while_an_edit_is_rechecked_the_draft_says_so_and_offers_no_buttons() -> None:
    from trailer_to_campaign.agents.checker import Verdict
    from trailer_to_campaign.approval import Recheck

    slack, client = approver()
    seen_during: list[dict[str, Any]] = []

    def recheck(text: str) -> Recheck:
        seen_during.append(client.updated[-1])
        return Recheck([], Verdict(True, []))

    slack.submit_edit("run1:post1", MARKETER, POST_EDIT)
    slack.click("t2c_reject", "run1:post1:v1", MARKETER)
    slack.social_post(1, 6, POST, recheck)
    [during] = seen_during
    assert buttons(during) == [] and "re-check" in text_of(during)


def test_a_draft_too_long_for_the_form_is_not_offered_for_editing() -> None:
    long_moment = KeyMoment(1.0, 2.0, "word " * 700, "calm", None, [], [1])
    slack, client = approver()
    thread, _ = in_background(lambda: slack.key_moment(1, 1, long_moment))
    assert client.draft_posted.wait(2)
    slack.open_editor(client, "trigger-1", "run1:km1", MARKETER)
    assert client.views == [] and "too long" in client.ephemeral[-1]["text"]
    slack.click("t2c_reject", "run1:km1", MARKETER)
    thread.join(2)


def test_an_edit_from_someone_who_is_not_a_marketer_changes_nothing() -> None:
    slack, client = approver()
    slack.submit_edit("run1:km1", PASSER_BY, KM_EDIT)
    slack.click("t2c_approve", "run1:km1", MARKETER)
    assert slack.key_moment(1, 5, MOMENT) == KeyMomentDecision("approve", by=MARKETER)
    assert client.ephemeral[0]["user"] == PASSER_BY


# T22: through a whole Run with the real SlackApprover, the record says what was approved over a flag.


def whole_run(
    tmp_path: Path, stages: Any, slack: SlackApprover, first_post: Callable[[], None]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Queue every click in the order the drafts are asked about: a click on another draft decides nothing."""
    import io
    import json
    from decimal import Decimal

    from trailer_to_campaign.pipeline import run_film

    for i in range(1, 6):
        slack.click("t2c_approve", f"run1:km{i}", MARKETER)
    first_post()
    for i in range(2, 7):
        slack.click("t2c_approve", f"run1:post{i}", MARKETER)
    run_film("sample", Decimal("2"), tmp_path / "runs", stages.stages(), slack, out=io.StringIO())
    [log] = (tmp_path / "runs").glob("sample/*/run.jsonl")
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    return lines, json.loads(log.with_name("campaign.json").read_text())["social_posts"]


def test_an_edit_approved_over_a_flag_in_slack_is_on_the_record(tmp_path: Path) -> None:
    from test_pipeline import FakeStages

    from trailer_to_campaign.agents.checker import Problem, Verdict
    from trailer_to_campaign.approval import Recheck

    edited = "An edited post. Stream it now."
    reason = "This gives away that he is captured."
    stages = FakeStages()
    stages.rechecks[edited] = Recheck([], Verdict(False, [Problem("spoiler", "edited", reason, spoiler_id="the-end")]))
    slack, client = approver()

    def first_post() -> None:
        slack.submit_edit("run1:post1", MARKETER, {"text": edited})
        slack.click("t2c_approve_light", "run1:post1:v1", MARKETER)

    lines, campaign = whole_run(tmp_path, stages, slack, first_post)

    assert campaign[0]["edit_check"] == "flagged" and any(reason in p for p in campaign[0]["edit_problems"])
    [rate] = [line for line in lines if line["event"] == "approval_rate"]
    assert rate["approved_over_a_flag"] == ["facebook en"]
    # The Checker's reasons can describe the spoiler (ADR 0007): the channel never sees them.
    assert all(reason not in str(m) for m in [*client.posted, *client.updated])
    assert any(reason in e["text"] for e in client.ephemeral if e["user"] == MARKETER)


def test_a_post_flagged_then_restored_in_slack_has_no_edit_on_the_record(tmp_path: Path) -> None:
    from test_pipeline import FakeStages

    from trailer_to_campaign.agents.checker import Problem, Verdict
    from trailer_to_campaign.approval import Recheck

    edited = "An edited post. Stream it now."
    stages = FakeStages()
    stages.rechecks[edited] = Recheck([], Verdict(False, [Problem("brand", "edited", "Tone.", rule="tone")]))
    slack, _ = approver()

    def first_post() -> None:
        slack.submit_edit("run1:post1", MARKETER, {"text": edited})
        slack.submit_edit("run1:post1:v1", MARKETER, {"text": "facebook en post. Stream it now."})  # the original
        slack.click("t2c_approve", "run1:post1:v2", MARKETER)

    lines, campaign = whole_run(tmp_path, stages, slack, first_post)

    assert campaign[0]["decision"] == "approve" and "edit_check" not in campaign[0]
    assert campaign[0]["final_text"] == "facebook en post. Stream it now."
    [rate] = [line for line in lines if line["event"] == "approval_rate"]
    assert rate["approved_over_a_flag"] == []
