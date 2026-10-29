"""Real Films' Spoiler Lists are unreleased plot: only the sample Film's file is in git."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def ignored(path: str) -> bool:
    return subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", path], check=False).returncode == 0


def test_a_real_films_file_is_kept_out_of_git() -> None:
    assert ignored("films/the-quiet-river/film.toml")


def test_the_sample_films_file_is_committed() -> None:
    assert not ignored("films/agent-327/film.toml")
    assert (ROOT / "films" / "agent-327" / "film.toml").is_file()


@pytest.mark.parametrize(
    "path",
    [
        "films/the-quiet-river/film.toml.bak",
        "films/the-quiet-river/film.toml~",
        "films/the-quiet-river/.film.toml.swp",
        "films/the-quiet-river/film.draft.toml",
        "films/the-quiet-river/notes.txt",
    ],
)
def test_nothing_in_a_real_films_folder_reaches_git(path: str) -> None:
    assert ignored(path)


def test_the_samples_footage_stays_out_of_git() -> None:
    assert ignored("films/agent-327/source/trailer.webm")
    assert ignored("films/agent-327/frames/abc123def456/frame_0000.jpg")
