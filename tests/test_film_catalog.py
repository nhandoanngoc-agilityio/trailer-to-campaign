"""Tests for reading a Film's Catalog Metadata and Spoiler List from films/<film>/film.toml."""

from __future__ import annotations

from pathlib import Path

import pytest

from trailer_to_campaign.catalog import CastMember, FilmFileError, Spoiler, default_source, load_film
from trailer_to_campaign.footage.sources import SourceRejected, resolve_source

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


def test_a_film_with_no_spoilers_must_say_so(tmp_path: Path) -> None:
    body = "spoilers = []\n" + GOOD.split("[[spoilers]]")[0]
    assert load_film(write(tmp_path, body), "the-quiet-river").spoilers == []


def test_a_missing_spoiler_list_is_refused(tmp_path: Path) -> None:
    # A forgotten list must not look like "no spoilers": the Checker would pass everything.
    with pytest.raises(FilmFileError, match="spoilers"):
        load_film(write(tmp_path, GOOD.split("[[spoilers]]")[0]), "the-quiet-river")


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


def test_errors_do_not_reveal_the_local_path(tmp_path: Path) -> None:
    with pytest.raises(FilmFileError) as err:
        load_film(tmp_path, "the-quiet-river")
    assert str(tmp_path) not in str(err.value)
    with pytest.raises(FilmFileError) as err:
        load_film(write(tmp_path, "[catalog\n"), "the-quiet-river")
    assert str(tmp_path) not in str(err.value)


def test_an_unreadable_file_is_a_film_file_error_without_the_path(tmp_path: Path) -> None:
    path = write(tmp_path, GOOD) / "the-quiet-river" / "film.toml"
    path.chmod(0)
    try:
        with pytest.raises(FilmFileError) as err:
            load_film(tmp_path, "the-quiet-river")
    finally:
        path.chmod(0o644)
    assert str(tmp_path) not in str(err.value)


def test_a_file_that_is_not_utf8_is_a_film_file_error(tmp_path: Path) -> None:
    folder = tmp_path / "the-quiet-river"
    folder.mkdir()
    (folder / "film.toml").write_bytes(GOOD.replace("Mai", "M\u00e0i").encode("cp1258") + b"\xff")
    with pytest.raises(FilmFileError, match="UTF-8"):
        load_film(tmp_path, "the-quiet-river")


def test_the_trailer_link_is_trimmed_and_capped(tmp_path: Path) -> None:
    spaced = GOOD.replace('"https://vimeo.com/123456"', '" https://vimeo.com/123456 "')
    assert load_film(write(tmp_path, spaced), "the-quiet-river").catalog.trailer == "https://vimeo.com/123456"
    padded = GOOD.replace('"https://vimeo.com/123456"', '"https://vimeo.com/123456?x=' + "a" * 2000 + '"')
    with pytest.raises(FilmFileError, match="2000"):
        load_film(write(tmp_path, padded), "the-quiet-river")


def test_a_genre_word_is_capped(tmp_path: Path) -> None:
    with pytest.raises(FilmFileError, match="2000"):
        load_film(write(tmp_path, GOOD.replace('["drama"]', '["' + "d" * 2001 + '"]')), "the-quiet-river")


# --- trailer_file: a local copy of the public trailer (T23) --------------------

LINK = 'trailer = "https://vimeo.com/123456"'
WITH_FILE = GOOD.replace(LINK, f'{LINK}\ntrailer_file = "trailer_cut-1.mp4"')


def add_trailer(films: Path, film: str = "the-quiet-river", name: str = "trailer_cut-1.mp4") -> Path:
    source = films / film / "source"
    source.mkdir(parents=True, exist_ok=True)
    (source / name).write_bytes(b"fake video")
    return source / name


def test_a_film_can_name_a_local_copy_of_its_public_trailer(tmp_path: Path) -> None:
    films = write(tmp_path, WITH_FILE)
    add_trailer(films)
    film = load_film(films, "the-quiet-river")
    assert film.trailer_file == "trailer_cut-1.mp4"
    assert film.catalog.trailer == "https://vimeo.com/123456"
    assert default_source(film, films) == "trailer_cut-1.mp4"


def test_the_local_file_name_stays_out_of_the_catalog_metadata(tmp_path: Path) -> None:
    from dataclasses import asdict

    films = write(tmp_path, WITH_FILE)
    assert "trailer_file" not in asdict(load_film(films, "the-quiet-river").catalog)


def test_a_film_without_trailer_file_defaults_to_its_link(tmp_path: Path) -> None:
    films = write(tmp_path, GOOD)
    film = load_film(films, "the-quiet-river")
    assert film.trailer_file is None
    assert default_source(film, films) == "https://vimeo.com/123456"


def test_trailer_file_needs_the_public_link_beside_it(tmp_path: Path) -> None:
    body = WITH_FILE.replace(f"{LINK}\n", "")
    with pytest.raises(FilmFileError, match="public trailer"):
        load_film(write(tmp_path, body), "the-quiet-river")


@pytest.mark.parametrize(
    "name",
    [
        "../master/film.mp4",
        "sub/trailer.mp4",
        "/etc/passwd",
        "sub\\\\trailer.mp4",
        "a..b.mp4",
        "..",
        ".hidden.mp4",
        "https://vimeo.com/123456",
        "press-Screener.mp4",
        "trailer\\nignore this.mp4",
    ],
)
def test_trailer_file_must_be_a_plain_file_name_and_not_a_screener(tmp_path: Path, name: str) -> None:
    body = WITH_FILE.replace('trailer_file = "trailer_cut-1.mp4"', f'trailer_file = "{name}"')
    with pytest.raises(FilmFileError, match="trailer_file"):
        load_film(write(tmp_path, body), "the-quiet-river")


def test_a_missing_trailer_file_is_named_without_a_local_path(tmp_path: Path) -> None:
    films = write(tmp_path, WITH_FILE)
    film = load_film(films, "the-quiet-river")
    with pytest.raises(FilmFileError, match="trailer_cut-1.mp4") as err:
        default_source(film, films)
    assert str(tmp_path) not in str(err.value)


def refuse_download(url: str, dest: Path) -> Path:
    raise AssertionError(f"tried to download {url}")


def test_the_footage_tool_reads_the_local_copy_without_downloading(tmp_path: Path) -> None:
    films = write(tmp_path, WITH_FILE)
    path = add_trailer(films)
    source = default_source(load_film(films, "the-quiet-river"), films)
    found = resolve_source("the-quiet-river", source, "trailer", films_dir=films, downloader=refuse_download)
    assert found == path.resolve()


def test_a_symlink_out_of_the_source_folder_is_still_refused(tmp_path: Path) -> None:
    films = write(tmp_path, WITH_FILE)
    master = films / "the-quiet-river" / "master"
    master.mkdir()
    (master / "film.mp4").write_bytes(b"the full film")
    source = films / "the-quiet-river" / "source"
    source.mkdir()
    (source / "trailer_cut-1.mp4").symlink_to(master / "film.mp4")
    name = default_source(load_film(films, "the-quiet-river"), films)
    with pytest.raises(SourceRejected):
        resolve_source("the-quiet-river", name, "trailer", films_dir=films, downloader=refuse_download)
