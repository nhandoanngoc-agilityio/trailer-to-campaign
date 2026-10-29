"""Tests for reading a Film's Catalog Metadata and Spoiler List from films/<film>/film.toml."""

from __future__ import annotations

from pathlib import Path

import pytest

from trailer_to_campaign.catalog import CastMember, FilmFileError, Spoiler, load_film

FILMS = Path(__file__).resolve().parent.parent / "films"

GOOD = """
[catalog]
title = "The Quiet River"
synopsis = "A ferry pilot finds a letter in the river."
genre = ["drama"]
language = "vi"
runtime_minutes = 104
trailer = "https://vimeo.com/123456"

[[catalog.cast]]
name = "Lan Pham"
character = "Mai"

[[spoilers]]
id = "letter-is-hers"
text = "Mai wrote the letter herself."
why = "the twist"
"""


def write(tmp_path: Path, body: str, film: str = "the-quiet-river") -> Path:
    folder = tmp_path / film
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "film.toml").write_text(body, encoding="utf-8")
    return tmp_path


def test_the_sample_film_loads() -> None:
    film = load_film(FILMS, "agent-327")
    assert film.catalog.title == "Agent 327: Operation Barbershop"
    assert film.catalog.language == "en"
    assert film.catalog.cast == [CastMember("Agent 327"), CastMember("Boris Kloris")]
    assert all(member.name is None for member in film.catalog.cast)  # no actor is guessed
    assert [s.id for s in film.spoilers] == ["ending-capture"]


def test_a_full_film_file_loads(tmp_path: Path) -> None:
    film = load_film(write(tmp_path, GOOD), "the-quiet-river")
    assert film.catalog.genre == ["drama"]
    assert film.catalog.runtime_minutes == 104
    assert film.catalog.cast == [CastMember("Mai", "Lan Pham")]
    assert film.spoilers == [Spoiler("letter-is-hers", "Mai wrote the letter herself.", "the twist")]


@pytest.mark.parametrize("field", ["title", "synopsis", "genre", "language", "runtime_minutes", "cast"])
def test_a_missing_catalog_field_is_named(tmp_path: Path, field: str) -> None:
    lines = [line for line in GOOD.splitlines() if not line.startswith(f"{field} =")]
    body = "\n".join(lines)
    if field == "cast":
        body = body.replace('[[catalog.cast]]\nname = "Lan Pham"\ncharacter = "Mai"\n', "")
    with pytest.raises(FilmFileError, match=field):
        load_film(write(tmp_path, body), "the-quiet-river")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('genre = ["drama"]', 'genre = "drama"'),
        ('genre = ["drama"]', "genre = []"),
        ("runtime_minutes = 104", "runtime_minutes = 0"),
        ("runtime_minutes = 104", 'runtime_minutes = "104"'),
        ('title = "The Quiet River"', 'title = "  "'),
        ('language = "vi"', 'language = "Vietnamese"'),
        ('character = "Mai"', 'character = ""'),
        ('trailer = "https://vimeo.com/123456"', 'trailer = "https://evil.io/t.mp4"'),
        ('id = "letter-is-hers"', 'id = "Letter Is Hers"'),
    ],
)
def test_a_wrong_value_is_refused(tmp_path: Path, old: str, new: str) -> None:
    with pytest.raises(FilmFileError):
        load_film(write(tmp_path, GOOD.replace(old, new)), "the-quiet-river")


@pytest.mark.parametrize("language", ["en", "vi", "pt-BR", "zh-Hant"])
def test_language_tags_are_accepted(tmp_path: Path, language: str) -> None:
    load_film(write(tmp_path, GOOD.replace('language = "vi"', f'language = "{language}"')), "the-quiet-river")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("synopsis =", "synopis ="),  # a typo must not silently drop the synopsis
        ("[[spoilers]]", "[[spoiler]]"),
        ('why = "the twist"', 'why = "the twist"\nseverity = "high"'),
    ],
)
def test_unknown_keys_are_refused(tmp_path: Path, old: str, new: str) -> None:
    with pytest.raises(FilmFileError, match="unknown"):
        load_film(write(tmp_path, GOOD.replace(old, new)), "the-quiet-river")


def test_spoiler_ids_are_unique(tmp_path: Path) -> None:
    twice = GOOD + '\n[[spoilers]]\nid = "letter-is-hers"\ntext = "Again."\n'
    with pytest.raises(FilmFileError, match="letter-is-hers"):
        load_film(write(tmp_path, twice), "the-quiet-river")


def test_a_film_may_have_no_spoilers(tmp_path: Path) -> None:
    body = GOOD.split("[[spoilers]]")[0]
    assert load_film(write(tmp_path, body), "the-quiet-river").spoilers == []


def test_long_text_is_refused(tmp_path: Path) -> None:
    long = GOOD.replace('"Mai wrote the letter herself."', '"' + "x" * 2001 + '"')
    with pytest.raises(FilmFileError, match="2000"):
        load_film(write(tmp_path, long), "the-quiet-river")


def test_a_huge_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FilmFileError, match="KB"):
        load_film(write(tmp_path, GOOD + "# " + "x" * 70_000 + "\n"), "the-quiet-river")


def test_a_missing_film_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FilmFileError, match="no film.toml"):
        load_film(tmp_path, "the-quiet-river")


def test_malformed_toml_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FilmFileError, match="TOML"):
        load_film(write(tmp_path, "[catalog\ntitle = "), "the-quiet-river")


@pytest.mark.parametrize("film", ["../agent-327", "Agent 327", ""])
def test_film_must_be_a_slug(film: str) -> None:
    with pytest.raises(ValueError, match="film"):
        load_film(FILMS, film)
