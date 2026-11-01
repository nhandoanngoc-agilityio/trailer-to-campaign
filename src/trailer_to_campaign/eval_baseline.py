"""What an Eval Set report was measured on, and finding `main`'s report again (T20).

`main`'s numbers don't change until an agent's prompt, tool or model changes on `main`, so a
saved report can stand in for a new run. The hash covers every .py and .md file of the agents,
their tools and the footage they watch, which holds the prompts, tool code and model names, and
what those read and are priced by. Nothing else moves it: not a test, a doc or evals.py.

It is built from each file's path and git blob id, so the same value comes from the files on
disk and from a git ref. That is what lets a branch that changed a prompt still find `main`'s
report. The Eval Set itself (Answer Key, film.toml) is not in it: Song Lang is gitignored, so
that part can't be compared with a ref. CI's answer-key job flags a change there.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

PACKAGE = Path(__file__).parent
# catalog.py feeds the catalog and Spoiler List tools, config.py names the Whisper model, pricing.py sets the cost.
AGENT_PATHS = ("agents", "tools", "footage", "catalog.py", "config.py", "pricing.py")
AGENT_SUFFIXES = {".py", ".md"}
MAIN_REFS = ("origin/main", "main")
GIT_TIMEOUT_S = 10

log = logging.getLogger(__name__)


def _is_agent_file(relative: str) -> bool:
    path = Path(relative)
    return path.suffix in AGENT_SUFFIXES and "__pycache__" not in path.parts


def _combined(entries: Iterable[tuple[str, str]]) -> str:
    lines = "".join(f"{path}\0{blob}\n" for path, blob in sorted(entries))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def agents_hash(package: Path = PACKAGE) -> str:
    """The hash of the agents as they are on disk."""
    entries = []
    for name in AGENT_PATHS:
        for path in [package / name, *(package / name).rglob("*")]:
            relative = path.relative_to(package).as_posix()
            if path.is_file() and _is_agent_file(relative):
                data = path.read_bytes()
                entries.append((relative, hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()))  # git's blob id
    return _combined(entries)


def agents_hash_at(ref: str, package: Path = PACKAGE) -> str | None:
    """The same hash for a git ref, or None if git can't answer."""
    listing = _git(["ls-tree", "-r", "-z", ref, "--", *AGENT_PATHS], package)
    if listing is None:
        return None
    entries = []
    for line in filter(None, listing.split("\0")):
        meta, relative = line.split("\t", 1)
        if _is_agent_file(relative):
            entries.append((relative, meta.split()[2]))
    return _combined(entries) if entries else None


def main_agents_hash() -> str | None:
    """The hash of `main`'s agents: as last fetched, or as local if there is no remote."""
    for ref in MAIN_REFS:
        if (found := agents_hash_at(ref)) is not None:
            return found
    return None


def current_commit(package: Path = PACKAGE) -> str | None:
    return _git(["rev-parse", "--verify", "HEAD"], package)


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as err:
        log.warning("git could not answer: %r", err)
        return None
    return done.stdout.strip("\n") if done.returncode == 0 else None


def baselines(reports_dir: Path, wanted_hash: str, watcher: str) -> Iterator[tuple[Path, dict[str, Any]]]:
    """The saved reports that can stand for these agents, newest first."""
    if not reports_dir.is_dir():
        return
    for path in sorted((p for p in reports_dir.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
        try:
            summary = json.loads((path / "summary.json").read_text(encoding="utf-8"))
            stands = _stands_for(summary, wanted_hash, watcher)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as err:
            log.debug("passing over %s: %r", path, err)
            continue
        if stands:
            yield path, summary


def find_baseline(reports_dir: Path, wanted_hash: str, watcher: str) -> tuple[Path, dict[str, Any]] | None:
    return next(baselines(reports_dir, wanted_hash, watcher), None)


def _stands_for(summary: dict[str, Any], wanted_hash: str, watcher: str) -> bool:
    """A whole, clean run of the same agents. A run cut short by Ctrl-C or the API's credit limit is not one."""
    ran = [film for film in summary["films"] if film["stop"] != "skipped"]
    return (
        summary.get("agents_hash") == wanted_hash
        and summary.get("mode") == "full"
        and summary.get("watcher") == watcher
        and not summary.get("interrupted")
        and not summary.get("not_started")
        and bool(ran)
        and all(film["stop"] == "completed" and not film.get("errors") for film in ran)
    )
