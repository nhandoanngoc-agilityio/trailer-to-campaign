"""Tests for the brand rules code can check: no shouting, few emojis, a call to watch."""

from __future__ import annotations

import pytest

from trailer_to_campaign.agents.brand import brand_problems

TITLE = "Agent 327: Operation Barbershop"


def test_a_post_in_the_brand_voice_has_no_problems() -> None:
    assert brand_problems(f"{TITLE}: a spy walks into a barbershop 💈 Stream it now.", "en", TITLE) == []
    assert brand_problems(f"{TITLE}: điệp viên vào tiệm cắt tóc 💈✂️ Xem ngay!", "vi", TITLE) == []


def test_a_shouted_word_is_refused() -> None:
    [problem] = brand_problems(f"{TITLE}: the most AMAZING spy ever. Watch it now.", "en", TITLE)
    assert "AMAZING" in problem


def test_short_acronyms_and_words_of_the_title_may_be_capitals() -> None:
    title = "RRR"
    assert brand_problems(f"{title} on TV and in the USA. Watch it now.", "en", title) == []
    assert brand_problems("BARBERSHOP 327 is here. Watch it now.", "en", "BARBERSHOP 327") == []


def test_more_than_two_emojis_are_refused() -> None:
    [problem] = brand_problems(f"{TITLE} 💈✂️🔥 Stream it now.", "en", TITLE)
    assert "3 emojis" in problem


@pytest.mark.parametrize(
    ("text", "language"),
    [(f"{TITLE}: a spy walks into a barbershop.", "en"), (f"{TITLE}: điệp viên vào tiệm cắt tóc.", "vi")],
)
def test_a_post_without_a_call_to_watch_is_refused(text: str, language: str) -> None:
    [problem] = brand_problems(text, language, TITLE)
    assert "call to watch" in problem
