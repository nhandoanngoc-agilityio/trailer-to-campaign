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

import queue
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from trailer_to_campaign.agents.key_moments import KeyMoment
from trailer_to_campaign.agents.review import CheckedPost
from trailer_to_campaign.approval import ApprovalAborted, KeyMomentDecision, PostDecision, Recheck

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
        lines += [f"⚠️ {escape(problem)}" for problem in checked.problems]
        decision, user = self._decide(f"post{i}", "\n".join(lines))
        return PostDecision(decision, by=user)

    def _decide(self, draft: str, text: str) -> tuple[str, str]:
        if self._closed:
            raise ApprovalAborted("the Slack app stopped before every draft was decided")
        section = {"type": "section", "text": {"type": "mrkdwn", "text": text}}
        value = f"{self.run_id}:{draft}"
        actions = {
            "type": "actions",
            "elements": [
                {"type": "button", "action_id": APPROVE, "value": value, "style": "primary", "text": _plain("Approve")},
                {"type": "button", "action_id": REJECT, "value": value, "style": "danger", "text": _plain("Reject")},
            ],
        }
        posted = self.client.chat_postMessage(
            channel=self.channel, thread_ts=self.thread_ts, text=text, blocks=[section, actions]
        )
        decision, user = self._wait(value)
        shown = {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{SHOWN[decision]} by <@{user}>"}]}
        self.client.chat_update(channel=self.channel, ts=posted["ts"], text=text, blocks=[section, shown])
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
        self.client.chat_postEphemeral(channel=self.channel, thread_ts=self.thread_ts, user=user, text=text)


def _plain(text: str) -> dict[str, str]:
    return {"type": "plain_text", "text": text}
