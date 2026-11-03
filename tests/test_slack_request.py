"""Tests for reading a Marketer's @mention: which Film, which trailer, or why no Run starts."""

from __future__ import annotations

from pathlib import Path

import pytest

from trailer_to_campaign.slack_request import Refusal, RunRequest, parse_request

TRAILER = "https://www.youtube.com/watch?v=mN0zPOpADL4&t=1"
FILM = """
[catalog]
title = "{title}"
synopsis = "A spy."
genre = ["comedy"]
language = "en"
runtime_minutes = 4
{trailer}

[[catalog.cast]]
character = "Agent 327"
{spoilers}
"""


def write_film(films: Path, slug: str, trailer: str | None = TRAILER, spoilers: bool = True) -> None:
    (films / slug).mkdir(parents=True)
    (films / slug / "film.toml").write_text(
        FILM.format(
            title=slug,
            trailer=f'trailer = "{trailer}"' if trailer else "",
            spoilers='[[spoilers]]\nid = "ending"\ntext = "He is caught."' if spoilers else "",
        ),
        encoding="utf-8",
    )


@pytest.fixture
def films(tmp_path: Path) -> Path:
    films = tmp_path / "films"
    write_film(films, "agent-327")
    write_film(films, "the-quiet-river", trailer=None)
    write_film(films, "no-list", trailer="https://vimeo.com/123", spoilers=False)
    return films


def test_a_slug_runs_the_films_catalog_trailer(films: Path) -> None:
    assert parse_request("<@U0BOT> agent-327", films) == RunRequest("agent-327", TRAILER)


def test_ordinary_words_around_the_slug_are_ignored(films: Path) -> None:
    assert parse_request("<@U0BOT> please run agent-327 for us", films) == RunRequest("agent-327", TRAILER)


def test_a_slug_and_a_link_run_that_link(films: Path) -> None:
    other = "https://youtu.be/abc123"
    assert parse_request(f"<@U0BOT> agent-327 <{other}>", films) == RunRequest("agent-327", other)


def test_a_known_link_alone_finds_its_film_even_as_slack_wraps_it(films: Path) -> None:
    wrapped = "<https://www.youtube.com/watch?v=mN0zPOpADL4&amp;t=1|youtube.com/watch?v=mN0z…>"
    assert parse_request(f"<@U0BOT> {wrapped}", films) == RunRequest("agent-327", TRAILER)


@pytest.mark.parametrize(
    ("text", "says"),
    [
        ("<@U0BOT>", "agent-327"),  # names nothing: lists the Films it can run
        ("<@U0BOT> agent-237", "agent-327"),  # a typo: lists the Films it can run
        ("<@U0BOT> <https://youtu.be/unknown>", "No Film"),
        ("<@U0BOT> the-quiet-river", "trailer"),
        ("<@U0BOT> no-list", "Spoiler List"),
        ("<@U0BOT> agent-327 <https://evil.example/t.mp4>", "official trailer host"),
        ("<@U0BOT> agent-327 the-quiet-river", "one Film"),
        ("<@U0BOT> agent-327 <https://youtu.be/a> <https://youtu.be/b>", "one trailer"),
    ],
)
def test_no_run_starts_without_one_film_one_trailer_and_its_spoiler_list(films: Path, text: str, says: str) -> None:
    result = parse_request(text, films)
    assert isinstance(result, Refusal)
    assert says in result.reason
    assert str(films) not in result.reason  # replies go to Slack: never a local path


# --- trailer_file: a local copy of the public trailer (T23) --------------------


@pytest.fixture
def with_file(tmp_path: Path) -> Path:
    films = tmp_path / "films"
    write_film(films, "city-film")
    toml = films / "city-film" / "film.toml"
    link = f'trailer = "{TRAILER}"'
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(link, f'{link}\ntrailer_file = "cut.mp4"'), encoding="utf-8"
    )
    (films / "city-film" / "source").mkdir()
    (films / "city-film" / "source" / "cut.mp4").write_bytes(b"fake video")
    return films


def test_a_slug_runs_the_films_local_trailer(with_file: Path) -> None:
    assert parse_request("<@U0BOT> city-film", with_file) == RunRequest("city-film", "cut.mp4")


def test_the_catalogs_own_link_with_the_slug_uses_the_local_trailer(with_file: Path) -> None:
    assert parse_request(f"<@U0BOT> city-film <{TRAILER}>", with_file) == RunRequest("city-film", "cut.mp4")


def test_the_catalogs_own_link_alone_uses_the_local_trailer(with_file: Path) -> None:
    assert parse_request(f"<@U0BOT> <{TRAILER}|the trailer>", with_file) == RunRequest("city-film", "cut.mp4")


def test_a_different_link_still_wins_over_the_local_trailer(with_file: Path) -> None:
    other = "https://vimeo.com/999"
    assert parse_request(f"<@U0BOT> city-film <{other}>", with_file) == RunRequest("city-film", other)


@pytest.mark.parametrize("text", ["<@U0BOT> city-film", f"<@U0BOT> <{TRAILER}>"])
def test_a_missing_local_trailer_starts_no_run_and_names_no_path(with_file: Path, text: str) -> None:
    (with_file / "city-film" / "source" / "cut.mp4").unlink()
    result = parse_request(text, with_file)
    assert isinstance(result, Refusal)
    assert "cut.mp4" in result.reason
    assert str(with_file) not in result.reason
