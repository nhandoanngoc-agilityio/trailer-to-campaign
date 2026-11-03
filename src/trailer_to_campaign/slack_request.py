"""What a Marketer's @mention asks for: one Film and its trailer, or why no Run starts.

    parse_request("<@U0BOT> agent-327", films_dir)             -> RunRequest("agent-327", <catalog trailer>)
    parse_request("<@U0BOT> agent-327 <https://youtu.be/…>")   -> RunRequest("agent-327", "https://youtu.be/…")
    parse_request("<@U0BOT> <https://www.youtube.com/…|…>")    -> the Film whose catalog trailer it is

The catalog's trailer is the Film's trailer_file when it has one, so a Run doesn't
depend on the video site answering; its own link counts as asking for that trailer.

A Run needs a Film with a valid film.toml, Spoiler List included: without the list
the Checker has nothing to guard, so no Run starts. The message is data: it is only
read for a Film's slug and a link, never sent to a model. Refusals are posted in
Slack, so they never name a local path.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

from trailer_to_campaign.catalog import Film, default_source, load_film
from trailer_to_campaign.footage.sources import SourceRejected, check_url
from trailer_to_campaign.run_log import FILM_SLUG

MENTION = re.compile(r"<@[A-Z0-9]+>")
LINK = re.compile(r"<(https?://[^>|]+)(?:\|[^>]*)?>")  # Slack sends links as <url> or <url|label>


@dataclass(frozen=True)
class RunRequest:
    film: str
    source: str


@dataclass(frozen=True)
class Refusal:
    reason: str


def parse_request(text: str, films_dir: Path) -> RunRequest | Refusal:
    text = MENTION.sub(" ", text)
    links = [html.unescape(url) for url in LINK.findall(text)]  # Slack escapes & as &amp;
    words = LINK.sub(" ", text).split()
    films = sorted({w for w in words if FILM_SLUG.fullmatch(w) and (films_dir / w / "film.toml").is_file()})
    usage = f"Name a Film and, if you like, its trailer: `@t2c {_example(films_dir)} <trailer link>`."

    if len(films) > 1:
        return Refusal(f"Please name one Film per Run; you named {', '.join(films)}.")
    if len(links) > 1:
        return Refusal("Please give one trailer per Run; your message has more than one link.")
    link = links[0] if links else None
    if link:
        try:
            check_url(link)
        except SourceRejected as err:
            return Refusal(f"That link can't be used: {err}.")

    if films:
        film = _load(films_dir, films[0])
        if isinstance(film, Refusal):
            return film
        if link and link != film.catalog.trailer:
            return RunRequest(film.slug, link)
        return _catalog_trailer(film, films_dir, usage)

    if link:
        for slug in _slugs(films_dir):
            film = _load(films_dir, slug)
            if isinstance(film, Film) and film.catalog.trailer == link:
                return _catalog_trailer(film, films_dir, usage)
        return Refusal(f"No Film has that trailer link in its catalog. {usage}")
    return Refusal(f"I couldn't tell which Film you mean. {usage} Films I can run: {', '.join(_slugs(films_dir))}.")


def _catalog_trailer(film: Film, films_dir: Path, usage: str) -> RunRequest | Refusal:
    try:
        source = default_source(film, films_dir)
    except ValueError as err:  # FilmFileError: its messages name the Film, never the path
        return Refusal(f"{err}.")
    if not source:
        return Refusal(f"{film.slug} has no trailer link in its catalog. Add one to your message. {usage}")
    return RunRequest(film.slug, source)


def _load(films_dir: Path, slug: str) -> Film | Refusal:
    try:
        return load_film(films_dir, slug)
    except ValueError as err:  # FilmFileError: its messages name the Film, never the path
        return Refusal(f"{slug} can't run yet: {err}.")


def _slugs(films_dir: Path) -> list[str]:
    return sorted(p.parent.name for p in films_dir.glob("*/film.toml") if FILM_SLUG.fullmatch(p.parent.name))


def _example(films_dir: Path) -> str:
    slugs = _slugs(films_dir)
    return slugs[0] if slugs else "<film>"
