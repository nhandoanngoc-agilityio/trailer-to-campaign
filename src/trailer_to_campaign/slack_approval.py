"""Approval in Slack: each draft is posted in the Run Thread with Approve, Edit and Reject.

    approver = SlackApprover(client, channel, thread_ts, marketers, run_id)
    approver.click(action_id, value, user)                     # a button click (the app calls this)
    approver.open_editor(client, trigger_id, value, user)      # Edit: open the form now (trigger_id expires fast)
    approver.submit_edit(value, user, fields)                  # the form was saved
    approver.close()                                           # the app is stopping

It is the approval.Approver of a Slack Run. The Run's worker thread posts a draft and
waits on it:
- Approve or Reject decides it.
- Edit opens a form filled with the draft's latest text (a Key Moment's description
  and mood, a post's text). A saved edit is measured (the share of words it changed)
  and, for a post, re-checked. The draft then offers Approve as Light Edit, Approve
  as heavy edit, Edit again, or Reject: the Marketer marks Light or heavy with the
  measured change in front of them.

Only a Marketer decides. Anyone else, or a click on another draft (an old message, a
double click, another Run), or a button out of step, is told so privately and decides
nothing. The decision then replaces the buttons, with who made it. Stopping the app
ends the wait with ApprovalAborted, never an approval.

Model-written text is escaped (<!channel> inside a post would ping people), and the
Checker's reasons, which can describe the spoiler, go only to the Marketers (ADR 0007).
"""

from __future__ import annotations

import logging
import queue
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from trailer_to_campaign.agents.key_moments import KeyMoment
from trailer_to_campaign.agents.review import CheckedPost
from trailer_to_campaign.approval import (
    ApprovalAborted,
    KeyMomentDecision,
    PostDecision,
    Recheck,
    key_moment_text,
    measured_change,
)

log = logging.getLogger(__name__)

SECTION_CHARS = 2_900  # Slack refuses a section block over 3,000 characters
INPUT_CHARS = 3_000  # Slack's limit for a text input

APPROVE, EDIT, REJECT = "t2c_approve", "t2c_edit", "t2c_reject"
APPROVE_LIGHT, APPROVE_HEAVY = "t2c_approve_light", "t2c_approve_heavy"
BUTTONS = (APPROVE, EDIT, REJECT, APPROVE_LIGHT, APPROVE_HEAVY)  # every button the app routes here
EDIT_FORM = "t2c_edit"  # the edit form's callback_id
_SUBMITTED = "t2c_edit_submitted"  # a saved form, queued like a click


@dataclass(frozen=True)
class _Click:
    action_id: str
    value: str  # "<run id>:<draft>", e.g. "run1:km3" or "run1:post2"
    user: str
    fields: dict[str, str] | None = None  # a saved form's values


@dataclass
class _Draft:
    base: str  # "<run id>:<draft>"
    fields: dict[str, str]  # what a Marketer can edit, as it now stands
    labels: dict[str, str]  # each field's label in the form
    render: Callable[[dict[str, str]], list[str]]  # the draft's lines in the thread, escaped
    measure: Callable[[dict[str, str]], str]  # the text the measured change is taken on
    original: dict[str, str] = field(init=False)
    version: int = 0  # bumped by each saved edit: a click or form always names the version it was made on
    ts: str = ""
    notes: list[str] = field(default_factory=list)  # after an edit: its measured change and re-check
    changed: float | None = None
    edited_by: str | None = None

    def __post_init__(self) -> None:
        self.original = dict(self.fields)

    @property
    def value(self) -> str:
        """What this version's buttons and form carry; an older version's clicks decide nothing."""
        return self.base if self.version == 0 else f"{self.base}:v{self.version}"

    @property
    def edited(self) -> bool:
        return self.fields != self.original


def escape(text: str) -> str:
    """Model-written text shown as plain text: no mentions, channel pings or links."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackApprover:
    def __init__(self, client: Any, channel: str, thread_ts: str, marketers: frozenset[str], run_id: str) -> None:
        self.client, self.channel, self.thread_ts = client, channel, thread_ts
        self.marketers, self.run_id = marketers, run_id
        self._clicks: queue.Queue[_Click | None] = queue.Queue()
        self._closed = False
        self._current: _Draft | None = None  # the draft waiting for a decision, read by open_editor

    def click(self, action_id: str, value: str, user: str) -> None:
        self._clicks.put(_Click(action_id, value, user))

    def submit_edit(self, value: str, user: str, fields: dict[str, str]) -> None:
        self._clicks.put(_Click(_SUBMITTED, value, user, dict(fields)))

    def close(self) -> None:
        self._closed = True
        self._clicks.put(None)

    def open_editor(self, client: Any, trigger_id: str, value: str, user: str) -> None:
        """Open the edit form at once: Slack's trigger_id expires within seconds of the click."""
        draft = self._current
        if user not in self.marketers:
            self._tell(user, "Only a Marketer can approve, edit or reject drafts in a Run.")
        elif draft is None or draft.value != value or self._closed:
            self._tell(user, _why_not(draft, value))
        elif any(len(text) > INPUT_CHARS for text in draft.fields.values()):
            # The form can't hold it, and cutting it would silently drop the tail on save.
            self._tell(user, "This draft is too long to edit in Slack's form: approve or reject it here.")
        else:
            try:
                client.views_open(trigger_id=trigger_id, view=_form(draft))
            except Exception:
                log.warning("could not open the edit form for %s", value, exc_info=True)
                self._tell(user, "The edit form couldn't open. Click Edit again.")

    def key_moment(self, i: int, n: int, moment: KeyMoment) -> KeyMomentDecision:
        def render(fields: dict[str, str]) -> list[str]:
            lines = [f"*Key Moment {i}/{n}* · {moment.start:.1f}–{moment.end:.1f} s · {escape(fields['mood'])}"]
            lines.append(escape(fields["what_happens"]))
            if moment.quote:
                lines.append(f"Quote: “{escape(moment.quote.text)}”")
            if moment.characters:
                lines.append(f"Characters: {escape(', '.join(moment.characters))}")
            return lines

        draft = _Draft(
            base=f"{self.run_id}:km{i}",
            fields={"what_happens": moment.what_happens, "mood": moment.mood},
            labels={"what_happens": "Description", "mood": "Mood"},
            render=render,
            measure=lambda f: key_moment_text(f["what_happens"], f["mood"]),
        )
        decision, user, light = self._decide(draft, recheck=None)
        if decision == "edit":
            return KeyMomentDecision(
                "edit",
                what_happens=draft.fields["what_happens"],
                mood=draft.fields["mood"],
                light=light,
                by=user,
                edited_by=draft.edited_by,
                changed=draft.changed,
            )
        return KeyMomentDecision(decision, by=user)

    def social_post(self, i: int, n: int, checked: CheckedPost, recheck: Callable[[str], Recheck]) -> PostDecision:
        post = checked.post
        status = "*FLAGGED*" if checked.status == "flagged" else checked.status
        header = f"*Social Post {i}/{n}* · {post.platform} {post.language} · {status} after {checked.retries} retries"
        if checked.problems:
            # The Checker's reasons can describe the spoiler (ADR 0007): the channel sees that
            # the post was flagged; only the Marketers, privately, see why.
            reasons = "\n".join(f"⚠️ {escape(problem)}" for problem in checked.problems)
            self._tell_marketers(f"Why Social Post {i}/{n} ({post.platform} {post.language}) was flagged:\n{reasons}")

        def render(fields: dict[str, str]) -> list[str]:
            lines = [header, escape(fields["text"])]
            if checked.problems:
                lines.append("⚠️ The Checker flagged this post. Marketers: the reasons were sent to you privately.")
            return lines

        draft = _Draft(
            base=f"{self.run_id}:post{i}",
            fields={"text": post.text},
            labels={"text": f"{post.platform} {post.language} post"},
            render=render,
            measure=lambda f: f["text"],
        )
        decision, user, light = self._decide(draft, recheck=recheck)
        if decision == "edit":
            return PostDecision(
                "edit",
                text=draft.fields["text"],
                light=light,
                by=user,
                edited_by=draft.edited_by,
                changed=draft.changed,
            )
        return PostDecision(decision, by=user)

    def _decide(self, draft: _Draft, recheck: Callable[[str], Recheck] | None) -> tuple[str, str, bool | None]:
        if self._closed:
            raise ApprovalAborted("the Slack app stopped before every draft was decided")
        self._current = draft  # before posting, so an Edit click on the new message finds it
        blocks = self._blocks(draft, buttons=self._buttons(draft))
        posted = self.client.chat_postMessage(
            channel=self.channel, thread_ts=self.thread_ts, text=_fallback(draft), blocks=blocks
        )
        draft.ts = posted["ts"]
        try:
            while True:
                click = self._next(draft)
                action, user = click.action_id, click.user
                if action == REJECT:
                    return self._final(draft, "reject", user, None)
                if action == APPROVE and not draft.edited:
                    return self._final(draft, "approve", user, None)
                if action in (APPROVE_LIGHT, APPROVE_HEAVY) and draft.edited:
                    return self._final(draft, "edit", user, action == APPROVE_LIGHT)
                if action == _SUBMITTED:
                    self._apply_edit(draft, click, recheck)
                    continue
                choices = "approve the edit as a Light Edit or a heavy edit" if draft.edited else "approve it"
                self._tell(user, f"That button doesn't fit this step: {choices}, edit it, or reject it.")
        finally:
            self._current = None

    def _apply_edit(self, draft: _Draft, click: _Click, recheck: Callable[[str], Recheck] | None) -> None:
        fields = {k: (click.fields or {}).get(k, "").strip() or v for k, v in draft.fields.items()}
        if fields == draft.fields:
            self._tell(click.user, "That edit changes nothing, so the draft stays as it is.")
            return
        draft.fields = fields
        draft.version += 1  # every button and form from before this edit is now out of date
        draft.notes, draft.changed, draft.edited_by = [], None, None
        if draft.edited:
            draft.changed = measured_change(draft.measure(draft.original), draft.measure(fields))
            draft.edited_by = click.user
            draft.notes.append(f"✏️ Edited by <@{click.user}>: this edit changed {draft.changed:.0%} of the words.")
            if recheck is not None:
                # The re-check takes a while: show the edit was received, with no buttons to click meanwhile.
                waiting = f"✏️ Edit received from <@{click.user}>: re-checking it…"
                self._update(draft, self._sections([*draft.render(fields), waiting]))
                draft.notes += self._recheck(recheck, draft.measure(fields))
            draft.notes.append(
                "Approve it as a Light Edit or a heavy edit (rewriting the hook is never light), "
                "edit it again, or reject it."
            )
        self._update(draft, self._blocks(draft, buttons=self._buttons(draft)))

    def _recheck(self, recheck: Callable[[str], Recheck], text: str) -> list[str]:
        """The re-check of an edit, for the thread: code's findings shown, the Checker's reasons kept private."""
        try:
            result = recheck(text)
        except Exception as err:  # a re-check informs the Marketer; it never ends their Run
            return [f"Not re-checked: {escape(type(err).__name__)}."]
        lines = [escape(result.note)] if result.note else []
        lines += [f"⚠️ {escape(problem)}" for problem in result.problems]
        if result.verdict is not None:
            if result.verdict.passed:
                lines.append("The Checker passed the edit.")
            else:
                lines.append("⚠️ The Checker flagged the edit. Marketers: the reasons were sent to you privately.")
                reasons = "\n".join(
                    f"⚠️ {p.kind} {p.spoiler_id or p.rule}: {escape(p.reason)} "
                    f"(the words {escape(repr(p.offending_text))})"
                    for p in result.verdict.problems
                )
                self._tell_marketers(f"Why the Checker flagged your edit:\n{reasons}")
        return lines

    def _final(self, draft: _Draft, decision: str, user: str, light: bool | None) -> tuple[str, str, bool | None]:
        if decision == "edit":
            kind = "a Light Edit" if light else "a heavy edit"
            shown = f"✏️ Approved as {kind} by <@{user}> ({draft.changed or 0:.0%} of the words changed)"
        else:
            shown = f"{'✅ Approved' if decision == 'approve' else '❌ Rejected'} by <@{user}>"
        context = {"type": "context", "elements": [{"type": "mrkdwn", "text": shown}]}
        self._update(draft, [*self._sections(draft.render(draft.fields)), context])
        return decision, user, light

    def _next(self, draft: _Draft) -> _Click:
        """The next click on this version of this draft by a Marketer; anything else is answered privately."""
        while True:
            click = self._clicks.get()
            if click is None or self._closed:
                raise ApprovalAborted("the Slack app stopped before every draft was decided")
            if click.user not in self.marketers:
                self._tell(click.user, "Only a Marketer can approve, edit or reject drafts in a Run.")
            elif click.value != draft.value:
                self._tell(click.user, _why_not(draft, click.value))
            else:
                return click

    def _buttons(self, draft: _Draft) -> dict[str, Any]:
        if draft.edited:
            shown = [
                (APPROVE_LIGHT, "Approve as Light Edit", "primary"),
                (APPROVE_HEAVY, "Approve as heavy edit", None),
                (EDIT, "Edit again", None),
                (REJECT, "Reject", "danger"),
            ]
        else:
            shown = [(APPROVE, "Approve", "primary"), (EDIT, "Edit", None), (REJECT, "Reject", "danger")]
        elements = []
        for action_id, text, style in shown:
            button: dict[str, Any] = {
                "type": "button",
                "action_id": action_id,
                "value": draft.value,
                "text": _plain(text),
            }
            if style:
                button["style"] = style
            elements.append(button)
        return {"type": "actions", "elements": elements}

    def _blocks(self, draft: _Draft, buttons: dict[str, Any]) -> list[dict[str, Any]]:
        return [*self._sections([*draft.render(draft.fields), *draft.notes]), buttons]

    @staticmethod
    def _sections(lines: list[str]) -> list[dict[str, Any]]:
        return [{"type": "section", "text": {"type": "mrkdwn", "text": part}} for part in _parts("\n".join(lines))]

    def _update(self, draft: _Draft, blocks: list[dict[str, Any]]) -> None:
        try:  # cosmetic: what was decided or edited is already kept, and a Slack hiccup must not lose it
            self.client.chat_update(channel=self.channel, ts=draft.ts, text=_fallback(draft), blocks=blocks)
        except Exception:
            log.warning("could not update the draft message %s", draft.ts, exc_info=True)

    def _tell(self, user: str, text: str) -> None:
        """A private note to one person; best-effort, so it never stops a Run waiting for Approval."""
        try:
            self.client.chat_postEphemeral(channel=self.channel, thread_ts=self.thread_ts, user=user, text=text)
        except Exception:
            log.warning("could not tell %s privately", user, exc_info=True)

    def _tell_marketers(self, text: str) -> None:
        for marketer in sorted(self.marketers):
            self._tell(marketer, text)


def _form(draft: _Draft) -> dict[str, Any]:
    """The edit form, filled with the draft's latest text. Inputs are plain text, so nothing is escaped."""
    blocks = [
        {
            "type": "input",
            "block_id": name,
            "label": _plain(draft.labels[name]),
            "element": {
                "type": "plain_text_input",
                "action_id": "value",
                "multiline": name != "mood",
                "initial_value": value,  # open_editor refuses a draft too long for the form
                "max_length": INPUT_CHARS,
            },
        }
        for name, value in draft.fields.items()
    ]
    return {
        "type": "modal",
        "callback_id": EDIT_FORM,
        "private_metadata": draft.value,
        "title": _plain("Edit the draft"),
        "submit": _plain("Save the edit"),
        "close": _plain("Cancel"),
        "blocks": blocks,
    }


def _why_not(draft: _Draft | None, value: str) -> str:
    """Why a click or form on this value doesn't count: an older version of the draft, or another draft."""
    if draft is not None and value.split(":v")[0] == draft.base:
        return "That draft changed since you saw it. Use its latest message, or click Edit again to start from it."
    return "That draft was already decided, or isn't up for Approval yet."


def _fallback(draft: _Draft) -> str:
    return "\n".join(draft.render(draft.fields))[:SECTION_CHARS]


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
