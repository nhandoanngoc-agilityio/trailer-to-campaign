"""Real Films' Spoiler Lists are unreleased plot: only the sample Film's file is in git."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def ignored(path: str) -> bool:
    return subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", path], check=False).returncode == 0


def test_a_real_films_file_is_kept_out_of_git() -> None:
    assert ignored("films/the-quiet-river/film.toml")


def test_the_sample_films_file_is_committed() -> None:
    assert not ignored("films/agent-327/film.toml")
    assert (ROOT / "films" / "agent-327" / "film.toml").is_file()
