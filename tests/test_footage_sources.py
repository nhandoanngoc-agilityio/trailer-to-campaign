"""Tests for turning a trailer link or file name into a local file, safely (ADR 0005)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trailer_to_campaign.footage import settings
from trailer_to_campaign.footage.sources import (
    NotATrailer,
    SourceRejected,
    YtDlpDownloader,
    check_url,
    resolve_source,
)


class SpyDownloader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def __call__(self, url: str, dest: Path) -> Path:
        self.calls.append((url, dest))
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / "abc123.mp4"
        path.write_bytes(b"video")
        return path


@pytest.fixture
def films(tmp_path: Path) -> Path:
    source = tmp_path / "sample" / "source"
    source.mkdir(parents=True)
    (source / "trailer.mp4").write_bytes(b"video")
    return tmp_path


def resolve(films: Path, source: str, kind: str | None = "trailer", downloader: Any = None) -> Path:
    return resolve_source("sample", source, kind, films_dir=films, downloader=downloader or SpyDownloader())


# --- kind and screener --------------------------------------------------------


@pytest.mark.parametrize("kind", ["screener", "Trailer", "", None, "teaser"])
def test_only_an_explicit_trailer_is_accepted(films: Path, kind: str | None) -> None:
    spy = SpyDownloader()
    with pytest.raises(NotATrailer):
        resolve(films, "https://www.youtube.com/watch?v=abc", kind=kind, downloader=spy)
    assert spy.calls == []


@pytest.mark.parametrize(
    "source", ["Screener-final.mp4", "the-film-SCREENER.mov", "https://vimeo.com/123?name=screener"]
)
def test_a_screener_name_is_refused_even_when_called_a_trailer(films: Path, source: str) -> None:
    spy = SpyDownloader()
    with pytest.raises(NotATrailer):
        resolve(films, source, downloader=spy)
    assert spy.calls == []


def test_not_a_trailer_is_a_kind_of_source_rejected() -> None:
    assert issubclass(NotATrailer, SourceRejected)


# --- local files --------------------------------------------------------------


def test_a_file_in_the_films_source_folder_is_accepted(films: Path) -> None:
    assert resolve(films, "trailer.mp4") == (films / "sample" / "source" / "trailer.mp4").resolve()


@pytest.mark.parametrize("source", ["../../etc/passwd", "/etc/passwd", "../source/../../x.mp4"])
def test_a_file_outside_the_films_source_folder_is_refused(films: Path, source: str) -> None:
    with pytest.raises(SourceRejected):
        resolve(films, source)


def test_a_symlink_out_of_the_source_folder_is_refused(films: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"x")
    (films / "sample" / "source" / "link.mp4").symlink_to(outside)
    with pytest.raises(SourceRejected):
        resolve(films, "link.mp4")


def test_a_missing_file_is_refused(films: Path) -> None:
    with pytest.raises(SourceRejected, match="no such file"):
        resolve(films, "missing.mp4")


def test_film_must_be_a_slug(films: Path) -> None:
    with pytest.raises(ValueError, match="film"):
        resolve_source("../sample", "trailer.mp4", "trailer", films_dir=films, downloader=SpyDownloader())


# --- links --------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc",
        "https://youtube.com/watch?v=abc",
        "https://m.youtube.com/watch?v=abc",
        "https://youtu.be/abc",
        "https://vimeo.com/123456",
        "https://player.vimeo.com/video/123456",
    ],
)
def test_allowlisted_https_links_are_accepted(url: str) -> None:
    check_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.youtube.com/watch?v=abc",
        "file:///etc/passwd",
        "ftp://vimeo.com/1",
        "https://youtube.com.evil.io/watch",
        "https://evil.io/?u=youtube.com",
        "https://notyoutube.com/watch",
        "https://youtube.com@evil.io/watch",
        "https://user:pw@youtube.com/watch",
        "https://youtube.com:8443/watch",
        "https://localhost/x.mp4",
        "https://127.0.0.1/x.mp4",
        "https://[::1]/x.mp4",
        "https://10.0.0.5/x.mp4",
        "https://169.254.169.254/latest/meta-data",
    ],
)
def test_other_links_are_refused(url: str) -> None:
    with pytest.raises(SourceRejected):
        check_url(url)


def test_a_link_is_downloaded_into_the_films_source_folder(films: Path) -> None:
    spy = SpyDownloader()
    path = resolve(films, "https://www.youtube.com/watch?v=abc", downloader=spy)
    assert spy.calls == [("https://www.youtube.com/watch?v=abc", films / "sample" / "source")]
    assert path.parent == films / "sample" / "source"


def test_a_refused_link_never_reaches_the_downloader(films: Path) -> None:
    spy = SpyDownloader()
    with pytest.raises(SourceRejected):
        resolve(films, "https://evil.io/trailer.mp4", downloader=spy)
    assert spy.calls == []


# --- yt-dlp -------------------------------------------------------------------


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL, recording options and what was asked of it."""

    instances: list[FakeYDL] = []
    info: dict[str, Any] = {}

    def __init__(self, params: dict[str, Any]) -> None:
        self.params = params
        self.downloaded = False
        FakeYDL.instances.append(self)

    def __enter__(self) -> FakeYDL:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
        assert download is False, "metadata must be checked before anything is downloaded"
        return dict(FakeYDL.info)

    def process_ie_result(self, info: dict[str, Any], download: bool = True) -> dict[str, Any]:
        self.downloaded = True
        path = Path(self.params["outtmpl"]["default"].replace("%(id)s", info["id"]).replace("%(ext)s", "mp4"))
        path.write_bytes(b"video")
        return {**info, "requested_downloads": [{"filepath": str(path)}]}


@pytest.fixture
def fake_ydl() -> type[FakeYDL]:
    FakeYDL.instances = []
    FakeYDL.info = {"id": "abc123", "title": "The Quiet River - Official Trailer", "duration": 150}
    return FakeYDL


def test_yt_dlp_is_restricted_to_youtube_and_vimeo_single_videos(tmp_path: Path, fake_ydl: type[FakeYDL]) -> None:
    path = YtDlpDownloader(ydl_factory=fake_ydl)("https://youtu.be/abc123", tmp_path)
    [ydl] = fake_ydl.instances
    assert ydl.params["allowed_extractors"] == ["youtube", "vimeo"]
    assert ydl.params["noplaylist"] is True
    assert ydl.params["max_filesize"] == settings.MAX_BYTES
    assert ydl.downloaded
    assert path == tmp_path / "abc123.mp4"


@pytest.mark.parametrize(
    "info",
    [
        {"title": "The Quiet River - Press Screener", "duration": 150},
        {"title": "Trailer", "duration": settings.MAX_DURATION_S + 1},
        {"title": "Trailer", "duration": None},
        {"title": "Trailer", "duration": 150, "is_live": True},
        {"title": "Trailer", "duration": 150, "_type": "playlist"},
    ],
)
def test_yt_dlp_refuses_before_downloading(tmp_path: Path, fake_ydl: type[FakeYDL], info: dict[str, Any]) -> None:
    fake_ydl.info = {"id": "abc123", **info}
    with pytest.raises(SourceRejected):
        YtDlpDownloader(ydl_factory=fake_ydl)("https://youtu.be/abc123", tmp_path)
    assert not fake_ydl.instances[0].downloaded


def test_an_oversized_file_is_refused(films: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MAX_BYTES", 4)
    with pytest.raises(SourceRejected, match="MB"):
        resolve(films, "trailer.mp4")
