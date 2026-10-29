"""Tests for tracing every Social Post's facts back to a Key Moment or the Catalog Metadata."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from trailer_to_campaign.agents.key_moments import KeyMoment, Quote, load_key_moments
from trailer_to_campaign.agents.social_posts import Claim, NotTraced, SocialPost, trace

TITLE = "Agent 327: Operation Barbershop"
CATALOG: dict[str, Any] = {
    "film": "agent-327",
    "title": TITLE,
    "synopsis": "Secret agent 327 follows a clue to a barbershop in Amsterdam.",
    "genre": ["animation", "action", "comedy"],
    "language": "en",
    "runtime_minutes": 4,
    "cast": [{"character": "Agent 327", "name": None}, {"character": "Boris Kloris", "name": None}],
    "trailer": None,
}
MOMENTS = [
    KeyMoment(
        10.4, 16.3, "Agent 327 reports in.", "cool", Quote("This is 327. I'm going in.", 12.6, 15.4), ["Agent 327"], [5]
    ),
    KeyMoment(
        84.0, 90.0, "A cape is drawn up.", "deadpan", Quote("Just a trim, please.", 86.0, 88.0), ["Agent 327"], [34]
    ),
    KeyMoment(92.0, 118.0, "A brawl in the barbershop.", "slapstick", None, ["Agent 327"], [36]),
]
EN = f'{TITLE}: a spy walks into a barbershop. "Just a trim, please." Watch it now.'
VI = f'{TITLE}: điệp viên bước vào tiệm cắt tóc. "Just a trim, please." (tạm dịch: Chỉ tỉa thôi.) Xem ngay.'
CLAIMS = [
    {"text": "He asks for just a trim", "source": "key_moment:2"},
    {"text": "The title", "source": "catalog:title"},
]


def post(platform: str, language: str, **over: Any) -> dict[str, Any]:
    return {
        "platform": platform,
        "language": language,
        "text": EN if language == "en" else VI,
        "claims": CLAIMS,
        **over,
    }


def six(**first: Any) -> list[dict[str, Any]]:
    posts = [post(p, lang) for p in ("facebook", "instagram", "tiktok") for lang in ("en", "vi")]
    posts[0] = {**posts[0], **first}
    return posts


def problems(posts: list[Any]) -> list[str]:
    with pytest.raises(NotTraced) as caught:
        trace(posts, MOMENTS, CATALOG)
    return caught.value.problems


def test_a_traced_set_of_six_is_accepted() -> None:
    posts = trace(six(), MOMENTS, CATALOG)
    assert [(p.platform, p.language) for p in posts] == [
        ("facebook", "en"),
        ("facebook", "vi"),
        ("instagram", "en"),
        ("instagram", "vi"),
        ("tiktok", "en"),
        ("tiktok", "vi"),
    ]
    assert posts[1] == SocialPost(
        "facebook", "vi", VI, [Claim("He asks for just a trim", "key_moment:2"), Claim("The title", "catalog:title")]
    )


def test_a_missing_post_is_refused() -> None:
    [problem] = problems(six()[:-1])
    assert "tiktok" in problem and "vi" in problem


def test_a_second_post_for_the_same_platform_and_language_is_refused() -> None:
    assert any("twice" in p for p in problems([*six(), post("facebook", "en")]))


@pytest.mark.parametrize(("field", "value"), [("platform", "myspace"), ("language", "fr")])
def test_an_unknown_platform_or_language_is_refused(field: str, value: str) -> None:
    assert any(value in p for p in problems(six(**{field: value})))


def test_a_tiktok_post_over_150_characters_is_refused() -> None:
    posts = six()
    posts[4] = post("tiktok", "en", text=EN + " " + "x" * (150 - len(EN)))
    [problem] = problems(posts)
    assert "tiktok" in problem and "150" in problem


def test_an_instagram_post_with_more_than_5_hashtags_is_refused() -> None:
    posts = six()
    posts[2] = post("instagram", "en", text=EN + " #a #b #c #d #e #f")
    [problem] = problems(posts)
    assert "hashtag" in problem


@pytest.mark.parametrize(
    "quoted",
    ['"Nobody leaves this barbershop."', '"JUST A TRIM, PLEASE."', '"Just a tri"', "“Just a trim, sir.”"],
)
def test_a_quote_that_is_not_a_key_moment_line_word_for_word_is_refused(quoted: str) -> None:
    [problem] = problems(six(text=f"{TITLE}. {quoted} Watch it now."))
    assert "quote" in problem


@pytest.mark.parametrize("quoted", ['"Just a trim"', "“Just a trim, please.”", '"please."'])
def test_a_verbatim_part_of_a_quote_is_accepted_in_straight_or_curly_quotes(quoted: str) -> None:
    trace(six(text=f"{TITLE}. {quoted} Watch it now."), MOMENTS, CATALOG)


def test_a_quote_must_be_cited_to_the_moment_it_is_from() -> None:
    [problem] = problems(six(claims=[{"text": "A brawl", "source": "key_moment:3"}]))
    assert "key_moment:2" in problem


def test_a_vietnamese_post_must_be_in_vietnamese() -> None:
    posts = six()
    posts[1] = post("facebook", "vi", text=EN)
    [problem] = problems(posts)
    assert "Vietnamese" in problem


def test_every_post_names_the_film_exactly() -> None:
    [problem] = problems(six(text='Agent 327! "Just a trim, please." Watch it now.'))
    assert TITLE in problem


@pytest.mark.parametrize(
    ("claims", "says"),
    [
        ([], "claim"),
        ([{"text": "x", "source": "key_moment:11"}], "key_moment:11"),
        ([{"text": "x", "source": "catalog:budget"}], "catalog:budget"),
        ([{"text": "x", "source": "a review"}], "a review"),
    ],
)
def test_every_claim_cites_a_source_that_exists(claims: list[Any], says: str) -> None:
    assert any(says in p for p in problems(six(claims=claims)))


def test_a_malformed_post_is_a_problem_not_a_crash() -> None:
    posts: list[Any] = six()
    posts[3] = {"platform": "instagram"}
    assert any("post 4" in p for p in problems(posts))


def test_key_moments_load_back_from_what_t2c_watch_writes(tmp_path: Path) -> None:
    path = tmp_path / "key_moments.json"
    path.write_text(json.dumps([asdict(m) for m in MOMENTS]), encoding="utf-8")
    assert load_key_moments(path) == MOMENTS


@pytest.mark.parametrize("content", ["not json", "{}", '[{"start": 1}]'])
def test_a_broken_key_moments_file_is_refused_without_its_path(tmp_path: Path, content: str) -> None:
    path = tmp_path / "secret-dir" / "key_moments.json"
    path.parent.mkdir()
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        load_key_moments(path)
    assert "secret-dir" not in str(caught.value)


# From /code-review: the checks must not be fooled by the title, the gloss, other quote marks or short titles.


@pytest.mark.parametrize(
    "text",
    [
        f'{TITLE}: a spy walks into a barbershop. "Just a trim, please." (tạm dịch: Chỉ tỉa thôi.) Stream it now.',
        f"{TITLE}: “Chỉ tỉa thôi” is not a Key Moment line. Stream it now.",
    ],
)
def test_a_vi_post_whose_only_vietnamese_is_a_gloss_or_quote_is_refused(text: str) -> None:
    posts = six()
    posts[1] = post("facebook", "vi", text=text)
    assert any("Vietnamese" in p for p in problems(posts))


def test_a_vietnamese_title_does_not_make_an_english_post_vietnamese() -> None:
    catalog = {**CATALOG, "title": "Bố Già"}
    posts = [{**p, "text": p["text"].replace(TITLE, "Bố Già")} for p in six()]
    posts[1] = {**posts[1], "text": 'Bố Già: one family, one secret. "Just a trim, please." Stream it now.'}
    with pytest.raises(NotTraced, match="Vietnamese"):
        trace(posts, MOMENTS, catalog)


@pytest.mark.parametrize("quoted", ["«Nobody leaves this barbershop»", "‘Nobody leaves’", "„Nobody leaves“"])
def test_quotes_in_other_quotation_marks_are_checked_too(quoted: str) -> None:
    [problem] = problems(six(text=f"{TITLE}. {quoted} Watch it now."))
    assert "Nobody leaves" in problem


def test_a_verbatim_quote_in_guillemets_is_accepted() -> None:
    trace(six(text=f"{TITLE}. «Just a trim, please.» Watch it now."), MOMENTS, CATALOG)


def test_a_short_title_must_be_a_whole_word() -> None:
    catalog = {**CATALOG, "title": "Her"}
    posts = [{**p, "text": p["text"].replace(TITLE, "Her")} for p in six()]
    posts[0] = {**posts[0], "text": 'Here is a spy. "Just a trim, please." Watch it now.'}
    with pytest.raises(NotTraced, match="exactly") as caught:
        trace(posts, MOMENTS, catalog)
    assert len(caught.value.problems) == 1


def test_a_title_written_in_another_unicode_form_still_counts() -> None:
    import unicodedata

    catalog = {**CATALOG, "title": unicodedata.normalize("NFC", "Bố Già")}
    nfd = unicodedata.normalize("NFD", "Bố Già")
    posts = [{**p, "text": p["text"].replace(TITLE, nfd)} for p in six()]
    trace(posts, MOMENTS, catalog)


def test_a_source_with_a_non_decimal_digit_is_reported_as_a_bad_source() -> None:
    [problem] = problems(six(claims=[*CLAIMS, {"text": "x", "source": "key_moment:²"}]))
    assert "key_moment:²" in problem
