"""Approval: a marketer's explicit decision on each Campaign Draft, given in the terminal.

    approver = TerminalApprover()
    approver.key_moment(i, n, moment)            -> KeyMomentDecision
    approver.social_post(i, n, checked, recheck) -> PostDecision

Each draft is approved, edited or rejected; an edit is then marked a Light Edit or
heavy (rewriting the hook is never light). An edited Social Post is re-checked
(tracing and the Checker) and the result shown before the marketer decides.

Only a keystroke approves. A closed terminal (EOF) raises ApprovalAborted and the
Run ends in an error; there is no default and no flag that approves for a person.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Protocol, TextIO

from trailer_to_campaign.agents.checker import Verdict
from trailer_to_campaign.agents.key_moments import KeyMoment
from trailer_to_campaign.agents.review import CheckedPost

END_OF_EDIT = "."
# Each answer is its key or its whole word, nothing else: "abort" must never read as "approve".
WORDS = {"a": "approve", "e": "edit", "r": "reject", "l": "light", "h": "heavy"}


def measured_change(before: str, after: str) -> float:
    """The share of words an edit changed: 0.0 unchanged, 1.0 rewritten. A check beside the Light/heavy mark.

    Words, not characters, so fixing a diacritic or a comma weighs like one word, as a reader sees it.
    """
    old, new = before.split(), after.split()
    return round(1 - SequenceMatcher(None, old, new, autojunk=False).ratio(), 3)


class ApprovalAborted(Exception):
    """The marketer's terminal closed mid-Approval: nothing more can be decided, so nothing is approved."""


@dataclass(frozen=True)
class KeyMomentDecision:
    decision: str  # approve, edit or reject
    what_happens: str | None = None  # set for an edit
    mood: str | None = None
    light: bool | None = None  # an edit only: True for a Light Edit, False for heavy
    by: str | None = None  # the Marketer who decided, when the surface knows (a Slack user id)


@dataclass(frozen=True)
class PostDecision:
    decision: str  # approve, edit or reject
    text: str | None = None  # the edited text
    light: bool | None = None
    by: str | None = None


@dataclass(frozen=True)
class Recheck:
    """The re-check of an edited post: tracing problems, and the Checker's verdict (None if it couldn't run)."""

    problems: list[str]
    verdict: Verdict | None
    note: str | None = None


class Approver(Protocol):
    def key_moment(self, i: int, n: int, moment: KeyMoment) -> KeyMomentDecision: ...

    def social_post(self, i: int, n: int, checked: CheckedPost, recheck: Callable[[str], Recheck]) -> PostDecision: ...


class TerminalApprover:
    def __init__(self, input: Callable[[str], str] = input, out: TextIO = sys.stdout) -> None:
        self._input, self.out = input, out

    def _say(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    def _read(self, prompt: str) -> str:
        try:
            return self._input(prompt)
        except EOFError:
            raise ApprovalAborted("the terminal closed before every draft was decided") from None

    def _choose(self, prompt: str, keys: str) -> str:
        while True:
            answer = self._read(prompt).strip().lower()
            for key in keys:
                if answer in (key, WORDS[key]):
                    return key

    def _light(self) -> bool:
        self._say("Is this a Light Edit? Rewriting the hook is never light.")
        return self._choose("[l]ight / [h]eavy: ", "lh") == "l"

    def key_moment(self, i: int, n: int, moment: KeyMoment) -> KeyMomentDecision:
        self._say(f"\n── Key Moment {i}/{n} · {moment.start:.1f}–{moment.end:.1f} s · {moment.mood}")
        self._say(moment.what_happens)
        if moment.quote:
            self._say(f'Quote: "{moment.quote.text}"')
        if moment.characters:
            self._say(f"Characters: {', '.join(moment.characters)}")
        choice = self._choose("[a]pprove / [e]dit / [r]eject: ", "aer")
        if choice == "a":
            return KeyMomentDecision("approve")
        if choice == "r":
            return KeyMomentDecision("reject")
        self._say("Edit the description and mood (Enter keeps them). Times, frames and the quote stay as watched.")
        what = self._read("Description: ").strip() or moment.what_happens
        mood = self._read("Mood: ").strip() or moment.mood
        if what == moment.what_happens and mood == moment.mood:
            return KeyMomentDecision("approve")
        return KeyMomentDecision("edit", what_happens=what, mood=mood, light=self._light())

    def social_post(self, i: int, n: int, checked: CheckedPost, recheck: Callable[[str], Recheck]) -> PostDecision:
        post = checked.post
        status = checked.status.upper() if checked.status == "flagged" else checked.status
        self._say(
            f"\n── Social Post {i}/{n} · {post.platform} {post.language} · {status} after {checked.retries} retries"
        )
        self._say(post.text)
        for problem in checked.problems:
            self._say(f"  ! {problem}")
        choice = self._choose("[a]pprove / [e]dit / [r]eject: ", "aer")
        if choice == "a":
            return PostDecision("approve")
        while choice == "e":
            text = self._edit()
            if not text:
                self._say("That edit is empty, so nothing was re-checked.")
                choice = self._choose("[a]pprove the post as it was / [e]dit / [r]eject: ", "aer")
                if choice == "a":
                    return PostDecision("approve")
                continue
            self._show(recheck(text))
            choice = self._choose("[a]pprove this edit / [e]dit again / [r]eject: ", "aer")
            if choice == "a":
                return PostDecision("edit", text=text, light=self._light())
        return PostDecision("reject")

    def _edit(self) -> str:
        self._say(f"Type the new post. End with a line holding only {END_OF_EDIT!r}.")
        lines = []
        while (line := self._read("")) != END_OF_EDIT:
            lines.append(line)
        return "\n".join(lines).strip()

    def _show(self, result: Recheck) -> None:
        self._say("Re-check of your edit:")
        if result.note:
            self._say(f"  {result.note}")
        for problem in result.problems:
            self._say(f"  ! {problem}")
        if result.verdict is not None:
            if result.verdict.passed:
                self._say("  The Checker passed it.")
            for p in result.verdict.problems:
                self._say(f"  ! {p.kind} {p.spoiler_id or p.rule}: {p.reason} (the words {p.offending_text!r})")
