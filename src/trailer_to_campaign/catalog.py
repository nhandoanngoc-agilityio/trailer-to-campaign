"""A Film's Catalog Metadata and Spoiler List, read from films/<film>/film.toml.

One file per Film, written by the Film's team and read-only to the agents: this
module only reads (tomllib cannot write). Every field is data for the agents,
never instructions. The file is checked strictly, because a typo that silently
dropped a field (or a spoiler) would reach a Campaign Draft unnoticed.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trailer_to_campaign.footage.sources import SourceRejected, check_url
from trailer_to_campaign.run_log import check_film_slug

MAX_FILE_BYTES = 64 * 1024
MAX_TEXT_CHARS = 2000  # a synopsis or spoiler, not a pasted document
LANGUAGE_TAG = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")  # en, vi, pt-BR, zh-Hant
SPOILER_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class FilmFileError(ValueError):
    """The Film's film.toml is missing, malformed, or has a field the agents can't trust."""


@dataclass(frozen=True)
class CastMember:
    character: str
    name: str | None = None  # the actor; left out when no source says who plays the part


@dataclass(frozen=True)
class Catalog:
    title: str
    synopsis: str
    genre: list[str]
    language: str
    runtime_minutes: int
    cast: list[CastMember]
    trailer: str | None = None


@dataclass(frozen=True)
class Spoiler:
    id: str
    text: str
    why: str | None = None


@dataclass(frozen=True)
class Film:
    slug: str
    catalog: Catalog
    spoilers: list[Spoiler] = field(default_factory=list)


def film_file(films_dir: Path, film: str) -> Path:
    return films_dir / check_film_slug(film) / "film.toml"


def load_film(films_dir: Path, film: str) -> Film:
    path = film_file(films_dir, film)
    if not path.is_file():
        raise FilmFileError(f"no film.toml for {film!r} in {films_dir}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise FilmFileError(f"{path} is over {MAX_FILE_BYTES // 1024} KB")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as err:
        raise FilmFileError(f"{path} is not valid TOML: {err}") from err

    _only(data, {"catalog", "spoilers"}, "film.toml")
    catalog = _table(data, "catalog", "film.toml")
    _only(catalog, {"title", "synopsis", "genre", "language", "runtime_minutes", "cast", "trailer"}, "[catalog]")

    language = _text(catalog, "language", "[catalog]")
    if not LANGUAGE_TAG.fullmatch(language):
        raise FilmFileError(f"[catalog] language must be a tag like 'en', 'vi' or 'pt-BR', got {language!r}")
    runtime = catalog.get("runtime_minutes")
    if isinstance(runtime, bool) or not isinstance(runtime, int) or runtime <= 0:
        raise FilmFileError(f"[catalog] runtime_minutes must be a whole number of minutes, got {runtime!r}")
    genre = catalog.get("genre")
    if not isinstance(genre, list) or not genre or not all(isinstance(g, str) and g.strip() for g in genre):
        raise FilmFileError('[catalog] genre must be a non-empty list of words, e.g. ["drama"]')

    trailer = catalog.get("trailer")
    if trailer is not None:
        if not isinstance(trailer, str):
            raise FilmFileError("[catalog] trailer must be a link")
        try:
            check_url(trailer)
        except SourceRejected as err:
            raise FilmFileError(f"[catalog] trailer: {err}") from err

    return Film(
        slug=film,
        catalog=Catalog(
            title=_text(catalog, "title", "[catalog]"),
            synopsis=_text(catalog, "synopsis", "[catalog]"),
            genre=[g.strip() for g in genre],
            language=language,
            runtime_minutes=runtime,
            cast=_cast(catalog),
            trailer=trailer,
        ),
        spoilers=_spoilers(data),
    )


def _cast(catalog: dict[str, Any]) -> list[CastMember]:
    entries = catalog.get("cast")
    if not isinstance(entries, list) or not entries:
        raise FilmFileError("[catalog] cast must list at least one [[catalog.cast]] entry")
    cast = []
    for i, entry in enumerate(entries):
        where = f"[[catalog.cast]] #{i + 1}"
        if not isinstance(entry, dict):
            raise FilmFileError(f"{where} must be a table")
        _only(entry, {"character", "name"}, where)
        cast.append(CastMember(_text(entry, "character", where), _text(entry, "name", where, required=False)))
    return cast


def _spoilers(data: dict[str, Any]) -> list[Spoiler]:
    entries = data.get("spoilers", [])
    if not isinstance(entries, list):
        raise FilmFileError("spoilers must be a list of [[spoilers]] entries")
    spoilers, seen = [], set()
    for i, entry in enumerate(entries):
        where = f"[[spoilers]] #{i + 1}"
        if not isinstance(entry, dict):
            raise FilmFileError(f"{where} must be a table")
        _only(entry, {"id", "text", "why"}, where)
        spoiler_id = _text(entry, "id", where)
        if not SPOILER_ID.fullmatch(spoiler_id):
            raise FilmFileError(f"{where} id must be a slug like 'ending-capture', got {spoiler_id!r}")
        if spoiler_id in seen:
            raise FilmFileError(f"spoiler id {spoiler_id!r} is used twice")
        seen.add(spoiler_id)
        spoilers.append(Spoiler(spoiler_id, _text(entry, "text", where), _text(entry, "why", where, required=False)))
    return spoilers


def _only(table: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise FilmFileError(f"{where} has unknown keys {unknown}; allowed: {sorted(allowed)}")


def _table(data: dict[str, Any], key: str, where: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise FilmFileError(f"{where} needs a [{key}] table")
    return value


def _text(table: dict[str, Any], key: str, where: str, required: bool = True) -> Any:
    value = table.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise FilmFileError(f"{where} {key} must be non-empty text")
    if len(value) > MAX_TEXT_CHARS:
        raise FilmFileError(f"{where} {key} is {len(value)} characters, over {MAX_TEXT_CHARS}")
    return value.strip()
