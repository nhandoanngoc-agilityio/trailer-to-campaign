"""Tests for Approval in the terminal: a marketer's keystrokes, and nothing else, decide each draft."""

from __future__ import annotations

import io
from collections.abc import Iterator

import pytest

from trailer_to_campaign.agents.checker import Problem, Verdict
from trailer_to_campaign.agents.key_moments import KeyMoment, Quote
from trailer_to_campaign.agents.review import CheckedPost
from trailer_to_campaign.agents.social_posts import SocialPost
from trailer_to_campaign.approval import (
    ApprovalAborted,
    KeyMomentDecision,
    PostDecision,
    Recheck,
    TerminalApprover,
)

MOMENT = KeyMoment(
    84.0, 90.0, "A cape is drawn up to his chin.", "deadpan", Quote("Just a trim, please.", 86, 88), ["Agent 327"], [34]
)
POST = CheckedPost(SocialPost("tiktok", "en", '"Just a trim, please." Stream it now.', []), "passed", [], 0)
FLAGGED = CheckedPost(
    SocialPost("facebook", "en", '"Doctor, carry on with the procedure." Stream it now.', []),
    "flagged",
    ["spoiler ending-capture: gives away the ending (the words 'carry on with the procedure')"],
    2,
)


class Keys:
    """Scripted keystrokes; running out is EOF, as when a terminal closes."""

    def __init__(self, *answers: str) -> None:
        self.answers: Iterator[str] = iter(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        try:
            return next(self.answers)
        except StopIteration:
            raise EOFError from None


def approver(*answers: str) -> tuple[TerminalApprover, Keys, io.StringIO]:
    keys, out = Keys(*answers), io.StringIO()
    return TerminalApprover(input=keys, out=out), keys, out


def no_recheck(text: str) -> Recheck:
    raise AssertionError("no edit, so no re-check")


@pytest.mark.parametrize(("key", "decision"), [("a", "approve"), ("r", "reject"), ("Approve", "approve")])
def test_a_key_moment_is_approved_or_rejected(key: str, decision: str) -> None:
    terminal, _, out = approver(key)
    assert terminal.key_moment(1, 5, MOMENT) == KeyMomentDecision(decision)
    assert "A cape is drawn up" in out.getvalue() and "Just a trim, please." in out.getvalue()


def test_a_bad_key_is_asked_again() -> None:
    terminal, keys, _ = approver("y", "", "r")
    assert terminal.key_moment(1, 5, MOMENT).decision == "reject"
    assert len(keys.prompts) == 3


def test_a_key_moment_edit_changes_only_its_description_and_mood() -> None:
    terminal, _, _ = approver("e", "He grins as the cape goes up.", "", "l")  # Enter keeps the mood
    assert terminal.key_moment(1, 5, MOMENT) == KeyMomentDecision(
        "edit", what_happens="He grins as the cape goes up.", mood="deadpan", light=True
    )


def test_a_key_moment_edit_that_changes_nothing_is_an_approval() -> None:
    terminal, _, _ = approver("e", "", "")
    assert terminal.key_moment(1, 5, MOMENT) == KeyMomentDecision("approve")


def test_a_post_is_approved_without_a_light_question() -> None:
    terminal, keys, _ = approver("a")
    assert terminal.social_post(1, 6, POST, no_recheck) == PostDecision("approve")
    assert len(keys.prompts) == 1


def test_an_edited_post_is_rechecked_shown_and_then_decided() -> None:
    rechecked: list[str] = []

    def recheck(text: str) -> Recheck:
        rechecked.append(text)
        verdict = Verdict(False, [Problem("brand", "Stream", "Ends weakly.", rule="tone")])
        return Recheck(problems=["end with a call to watch"], verdict=verdict)

    terminal, _, out = approver("e", "A spy walks into a barbershop.", "Stream it now.", ".", "a", "h")
    decision = terminal.social_post(1, 6, POST, recheck)
    assert rechecked == ["A spy walks into a barbershop.\nStream it now."]
    assert decision == PostDecision("edit", text="A spy walks into a barbershop.\nStream it now.", light=False)
    shown = out.getvalue()
    assert "end with a call to watch" in shown and "Ends weakly." in shown  # the re-check, before the decision


def test_an_edit_can_be_edited_again_or_rejected() -> None:
    terminal, _, _ = approver("e", "First try.", ".", "e", "Second try.", ".", "r")
    seen: list[str] = []
    decision = terminal.social_post(1, 6, POST, lambda t: seen.append(t) or Recheck([], Verdict(True, [])))
    assert decision == PostDecision("reject")
    assert seen == ["First try.", "Second try."]


def test_a_flagged_post_shows_its_problems() -> None:
    terminal, _, out = approver("r")
    terminal.social_post(1, 6, FLAGGED, no_recheck)
    assert "FLAGGED" in out.getvalue() and "gives away the ending" in out.getvalue()  # loud, so it isn't missed


def test_a_recheck_that_could_not_run_says_so() -> None:
    terminal, _, out = approver("e", "New text.", ".", "a", "l")
    terminal.social_post(1, 6, POST, lambda t: Recheck([], None, note="not re-checked: Film Budget reached"))
    assert "not re-checked: Film Budget reached" in out.getvalue()


@pytest.mark.parametrize("answers", [(), ("e", "He grins."), ("e", "half an edit")])
def test_a_closed_terminal_never_becomes_an_approval(answers: tuple[str, ...]) -> None:
    terminal, _, _ = approver(*answers)
    with pytest.raises(ApprovalAborted):
        terminal.key_moment(1, 5, MOMENT)
    with pytest.raises(ApprovalAborted):
        approver(*answers)[0].social_post(1, 6, POST, lambda t: Recheck([], Verdict(True, [])))


# From /code-review: only the exact key or word decides; an empty edit is never a post.


@pytest.mark.parametrize("answer", ["abort", "actually reject", "any spoilers?", "ap", "r please"])
def test_a_word_that_merely_starts_with_a_key_decides_nothing(answer: str) -> None:
    terminal, keys, _ = approver(answer, "r")
    assert terminal.key_moment(1, 5, MOMENT).decision == "reject"
    assert len(keys.prompts) == 2  # asked again


@pytest.mark.parametrize(
    ("answer", "decision"), [("a", "approve"), ("A", "approve"), (" approve ", "approve"), ("reject", "reject")]
)
def test_the_key_or_the_whole_word_decides(answer: str, decision: str) -> None:
    terminal, _, _ = approver(answer)
    assert terminal.key_moment(1, 5, MOMENT).decision == decision


def test_an_empty_edit_is_asked_again_and_never_rechecked() -> None:
    rechecked: list[str] = []
    terminal, _, out = approver("e", ".", "e", "Real text. Stream it now.", ".", "a", "l")  # empty: choose again
    decision = terminal.social_post(1, 6, POST, lambda t: rechecked.append(t) or Recheck([], Verdict(True, [])))
    assert rechecked == ["Real text. Stream it now."]  # the empty one cost nothing
    assert decision.text == "Real text. Stream it now."
    assert "empty" in out.getvalue()


# T10: the measured change, the check beside a Marketer's Light/heavy mark.


@pytest.mark.parametrize(
    ("before", "after", "changed"),
    [
        ("A spy walks in. Stream it now.", "A spy walks in. Stream it now.", 0.0),
        ("one two three four five six seven eight nine ten", "one two three four five six seven eight nine TEN", 0.1),
        ("A spy walks in.", "Completely different words here now", 1.0),
        ("", "", 0.0),
        ("", "New text.", 1.0),
    ],
)
def test_the_measured_change_is_the_share_of_words_changed(before: str, after: str, changed: float) -> None:
    from trailer_to_campaign.approval import measured_change

    assert measured_change(before, after) == pytest.approx(changed, abs=0.01)


def test_the_terminal_shows_the_measured_change_before_light_or_heavy() -> None:
    terminal, keys, out = approver("e", "A cape is drawn up to his chin, slowly.", "", "l")
    terminal.key_moment(1, 5, MOMENT)
    shown = out.getvalue()
    assert "changed" in shown and "% of the words" in shown
    assert shown.index("% of the words") < shown.rindex("Light Edit")  # seen before the mark is asked

    terminal, _, out = approver("e", "A spy walks in. Stream it now.", ".", "a", "h")
    terminal.social_post(1, 6, POST, lambda t: Recheck([], Verdict(True, [])))
    assert "% of the words" in out.getvalue()


def test_spacing_alone_is_not_a_change() -> None:
    from trailer_to_campaign.approval import measured_change

    assert measured_change("A spy  walks in.\n", "A spy walks in.") == 0.0
