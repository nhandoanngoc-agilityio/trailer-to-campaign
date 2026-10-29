"""Turn a trailer link or file name into a local file, or refuse it.

A source is data from a caller, never trusted: it is checked before anything is
read, downloaded or handed to ffmpeg. Only an explicit `kind="trailer"` gets in
(ADR 0005). Local files must already be in films/<film>/source/. Links must be
https on an allowlisted host, and are fetched by yt-dlp limited to its YouTube
and Vimeo single-video extractors, after the metadata passes the same checks.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from trailer_to_campaign.footage import settings
from trailer_to_campaign.run_log import check_film_slug


class SourceRejected(ValueError):
    """The source is not something T2 will touch."""


class NotATrailer(SourceRejected):
    """Slice 1 is official trailers only; screeners are never sent to a model (ADR 0005)."""


class Downloader(Protocol):
    def __call__(self, url: str, dest: Path) -> Path: ...


def check_kind(kind: str | None) -> None:
    if kind != "trailer":
        raise NotATrailer(f'only kind="trailer" is accepted, got {kind!r}')


def check_name(name: str) -> None:
    if "screener" in name.lower():
        raise NotATrailer(f"{name!r} looks like a screener")


def check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise SourceRejected(f"only https links are accepted, got {parts.scheme or 'none'!r}")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise SourceRejected("links with a user name or password are refused")
    try:
        port = parts.port
    except ValueError as err:
        raise SourceRejected(f"bad port in {url!r}") from err
    if port not in (None, 443):
        raise SourceRejected(f"port {port} is refused")
    host = (parts.hostname or "").lower().rstrip(".")
    if not any(host == allowed or host.endswith("." + allowed) for allowed in settings.ALLOWED_HOSTS):
        raise SourceRejected(f"{host or url!r} is not an official trailer host ({', '.join(settings.ALLOWED_HOSTS)})")


def check_size(path: Path) -> None:
    size = path.stat().st_size
    if size > settings.MAX_BYTES:
        raise SourceRejected(f"{path.name} is {size / 2**20:.0f} MB, over {settings.MAX_BYTES / 2**20:.0f} MB")


def source_dir(films_dir: Path, film: str) -> Path:
    return films_dir / film / "source"


def resolve_source(film: str, source: str, kind: str | None, films_dir: Path, downloader: Downloader) -> Path:
    """Check everything about the request, then return the trailer as a local file."""
    check_film_slug(film)
    check_kind(kind)
    check_name(source)
    dest = source_dir(films_dir, film)
    if "://" in source:
        check_url(source)
        path = downloader(source, dest)
        check_name(path.name)
    else:
        path = _local(dest, source)
    check_size(path)
    return path


def _local(dest: Path, source: str) -> Path:
    root = dest.resolve()
    path = (dest / source).resolve()  # an absolute source replaces dest, then fails the check below
    if not path.is_relative_to(root):
        raise SourceRejected(f"local trailers must be in {dest}/, got {source!r}")
    if not path.is_file():
        raise SourceRejected(f"no such file in {dest}/: {source!r}")
    return path


def _youtube_dl(params: dict[str, Any]) -> Any:
    from yt_dlp import YoutubeDL

    return YoutubeDL(params)


class YtDlpDownloader:
    def __init__(self, ydl_factory: Callable[[dict[str, Any]], Any] = _youtube_dl) -> None:
        self.ydl_factory = ydl_factory

    def __call__(self, url: str, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        params = {
            "allowed_extractors": settings.ALLOWED_EXTRACTORS,
            "noplaylist": True,
            "max_filesize": settings.MAX_BYTES,
            "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
            "outtmpl": {"default": str(dest / "%(id)s.%(ext)s")},
            "restrictfilenames": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        with self.ydl_factory(params) as ydl:
            info = ydl.extract_info(url, download=False)
            _check_info(info)
            result = ydl.process_ie_result(info, download=True)
        [download] = result.get("requested_downloads") or [{}]
        path = Path(download.get("filepath") or "")
        if not path.is_file() or not path.resolve().is_relative_to(dest.resolve()):
            raise SourceRejected(f"the download of {url!r} did not produce a file in {dest}/")
        return path


def _check_info(info: dict[str, Any]) -> None:
    if info.get("_type") not in (None, "video"):
        raise SourceRejected("only a single video is accepted, not a playlist or channel")
    if info.get("is_live"):
        raise SourceRejected("live streams are refused")
    check_name(str(info.get("title") or ""))
    duration = info.get("duration")
    if not isinstance(duration, int | float) or duration <= 0:
        raise SourceRejected("the video's length is unknown, so it is refused")
    if duration > settings.MAX_DURATION_S:
        raise SourceRejected(f"the video is {duration:.0f} s, over the {settings.MAX_DURATION_S} s trailer limit")
