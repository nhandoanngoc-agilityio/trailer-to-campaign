"""Approval in Slack: each draft is posted in the Run Thread with Approve and Reject buttons.

    approver = SlackApprover(client, channel, thread_ts, marketers, run_id)
    approver.click(action_id, value, user)    # called by the app for each button click
    approver.close()                          # the app is stopping

It is the approval.Approver of a Slack Run. The Run's worker thread posts a draft and
waits for a click on that draft's buttons. Only a Marketer's click decides it; anyone
else, or a click on another draft (an old message, a double click, another Run), is
told so privately and decides nothing. The decision then replaces the buttons, with
who made it. Stopping the app ends the wait with ApprovalAborted, never an approval.
Edit, Light/heavy and the measured change come in T10.

Every draft is model-written, so its text is escaped: in Slack, <!channel> or <@U…>
inside a post would otherwise ping people.
"""

from __future__ import annotations

import logging
import queue
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from trailer_to_campaign.agents.key_moments import KeyMoment
from trailer_to_campaign.agents.review import CheckedPost
from trailer_to_campaign.approval import ApprovalAborted, KeyMomentDecision, PostDecision, Recheck

log = logging.getLogger(__name__)

SECTION_CHARS = 2_900  # Slack refuses a section block over 3,000 characters
APPROVE, REJECT = "t2c_approve", "t2c_reject"
DECISIONS = {APPROVE: "approve", REJECT: "reject"}
SHOWN = {"approve": "✅ Approved", "reject": "❌ Rejected"}


@dataclass(frozen=True)
class _Click:
    action_id: str
    value: str  # "<run id>:<draft>", e.g. "run1:km3" or "run1:post2"
    user: str


def escape(text: str) -> str:
    """Model-written text shown as plain text: no mentions, channel pings or links."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackApprover:
    def __init__(self, client: Any, channel: str, thread_ts: str, marketers: frozenset[str], run_id: str) -> None:
        self.client, self.channel, self.thread_ts = client, channel, thread_ts
        self.marketers, self.run_id = marketers, run_id
        self._clicks: queue.Queue[_Click | None] = queue.Queue()
        self._closed = False

    def click(self, action_id: str, value: str, user: str) -> None:
        self._clicks.put(_Click(action_id, value, user))

    def close(self) -> None:
        self._closed = True
        self._clicks.put(None)

    def key_moment(self, i: int, n: int, moment: KeyMoment) -> KeyMomentDecision:
        lines = [f"*Key Moment {i}/{n}* · {moment.start:.1f}–{moment.end:.1f} s · {escape(moment.mood)}"]
        lines.append(escape(moment.what_happens))
        if moment.quote:
            lines.append(f"Quote: “{escape(moment.quote.text)}”")
        if moment.characters:
            lines.append(f"Characters: {escape(', '.join(moment.characters))}")
        decision, user = self._decide(f"km{i}", "\n".join(lines))
        return KeyMomentDecision(decision, by=user)

    def social_post(self, i: int, n: int, checked: CheckedPost, recheck: Callable[[str], Recheck]) -> PostDecision:
        post = checked.post
        status = "*FLAGGED*" if checked.status == "flagged" else checked.status
        lines = [f"*Social Post {i}/{n}* · {post.platform} {post.language} · {status} after {checked.retries} retries"]
        lines.append(escape(post.text))
        if checked.problems:
            # The Checker's reasons can describe the spoiler (ADR 0007): the channel sees that
            # the post was flagged; only the Marketers, privately, see why.
            lines.append("⚠️ The Checker flagged this post. Marketers: the reasons were sent to you privately.")
            reasons = "\n".join(f"⚠️ {escape(problem)}" for problem in checked.problems)
            for marketer in sorted(self.marketers):
                self._tell(
                    marketer, f"Why Social Post {i}/{n} ({post.platform} {post.language}) was flagged:\n{reasons}"
                )
        decision, user = self._decide(f"post{i}", "\n".join(lines))
        return PostDecision(decision, by=user)

    def _decide(self, draft: str, text: str) -> tuple[str, str]:
        if self._closed:
            raise ApprovalAborted("the Slack app stopped before every draft was decided")
        sections = [{"type": "section", "text": {"type": "mrkdwn", "text": part}} for part in _parts(text)]
        value = f"{self.run_id}:{draft}"
        actions = {
            "type": "actions",
            "elements": [
                {"type": "button", "action_id": APPROVE, "value": value, "style": "primary", "text": _plain("Approve")},
                {"type": "button", "action_id": REJECT, "value": value, "style": "danger", "text": _plain("Reject")},
            ],
        }
        posted = self.client.chat_postMessage(
            channel=self.channel, thread_ts=self.thread_ts, text=text[:SECTION_CHARS], blocks=[*sections, actions]
        )
        decision, user = self._wait(value)
        shown = {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{SHOWN[decision]} by <@{user}>"}]}
        try:  # cosmetic: the decision is already made, and a Slack hiccup must not lose it
            self.client.chat_update(
                channel=self.channel, ts=posted["ts"], text=text[:SECTION_CHARS], blocks=[*sections, shown]
            )
        except Exception:
            log.warning("could not show the decision on %s", posted.get("ts"), exc_info=True)
        return decision, user

    def _wait(self, value: str) -> tuple[str, str]:
        while True:
            click = self._clicks.get()
            if click is None or self._closed:
                raise ApprovalAborted("the Slack app stopped before every draft was decided")
            if click.user not in self.marketers:
                self._tell(click.user, "Only a Marketer can approve or reject drafts in a Run.")
            elif click.value != value or click.action_id not in DECISIONS:
                self._tell(click.user, "That draft was already decided, or isn't up for Approval yet.")
            else:
                return DECISIONS[click.action_id], click.user

    def _tell(self, user: str, text: str) -> None:
        """A private note to one person; best-effort, so it never stops a Run waiting for Approval."""
        try:
            self.client.chat_postEphemeral(channel=self.channel, thread_ts=self.thread_ts, user=user, text=text)
        except Exception:
            log.warning("could not tell %s privately", user, exc_info=True)


def _plain(text: str) -> dict[str, str]:
    return {"type": "plain_text", "text": text}


def _parts(text: str) -> list[str]:
    """The text in pieces Slack accepts in one section block, split at line ends where it can."""
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > SECTION_CHARS:  # one very long line: cut it hard
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:SECTION_CHARS])
            line = line[SECTION_CHARS:]
        if current and len(current) + 1 + len(line) > SECTION_CHARS:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts
