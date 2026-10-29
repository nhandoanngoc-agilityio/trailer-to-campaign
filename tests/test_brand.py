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


# From /code-review: the Checker is told code already checked these, so code must count and match them right.


@pytest.mark.parametrize("emojis", ["🇻🇳❤️", "👍🏽💈", "👨‍👩‍👧✂️"])
def test_a_flag_a_skin_tone_or_a_family_is_one_emoji(emojis: str) -> None:
    assert brand_problems(f"{TITLE} {emojis} Stream it now.", "en", TITLE) == []


@pytest.mark.parametrize("emojis", ["⭐⭐⭐", "⌚⏳💈"])
def test_emojis_outside_the_common_blocks_are_counted(emojis: str) -> None:
    [problem] = brand_problems(f"{TITLE} {emojis} Stream it now.", "en", TITLE)
    assert "3 emojis" in problem


@pytest.mark.parametrize(
    ("text", "title"),
    [
        (f"{TITLE}: the watcher in the barbershop.", TITLE),
        (f"{TITLE}: a livestream gone wrong.", TITLE),
        ("The Watch: a spy walks into a barbershop.", "The Watch"),
        (f"Watch the trailer of {TITLE}. A spy walks into a barbershop.", TITLE),
    ],
)
def test_the_call_to_watch_is_a_whole_word_of_the_posts_own_last_sentence(text: str, title: str) -> None:
    [problem] = brand_problems(text, "en", title)
    assert "call to watch" in problem


def test_the_call_to_watch_may_come_before_hashtags_and_emojis() -> None:
    text = f"{TITLE}: a spy walks into a barbershop. Stream it now ✂️\n\n#Agent327 #Animation"
    assert brand_problems(text, "en", TITLE) == []


def test_capitals_inside_a_quote_or_from_the_catalog_are_not_shouting() -> None:
    text = f'{TITLE}: "RUN, DOCTOR" in IMAX. Stream it now.'
    assert brand_problems(text, "en", TITLE, allowed_caps={"IMAX"}) == []
    [problem] = brand_problems(text, "en", TITLE)
    assert "IMAX" in problem and "DOCTOR" not in problem
