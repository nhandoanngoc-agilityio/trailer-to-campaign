"""The run log: one JSON line per event, at runs/<film>/<UTC timestamp>/run.jsonl.

Each line is written and flushed as it happens, so a crashed or stopped run
still leaves a log of what it spent. Money is written as a string to keep
Decimal precision.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# How a Run's command exits when the Run doesn't complete, the same for every command.
EXIT_AGENT_FAILED = 1
EXIT_FILM_BUDGET_REACHED = 3

# A film slug becomes a directory name, so it must not be able to leave runs/.
FILM_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def check_film_slug(film: str) -> str:
    if not FILM_SLUG.fullmatch(film):
        raise ValueError(f"film must be a lowercase slug like 'the-quiet-river', got {film!r}")
    return film


def _json_default(value: Any) -> str:
    if isinstance(value, Decimal | Path):
        return str(value)
    raise TypeError(f"can't write {type(value).__name__} to the run log")


@dataclass
class RunLog:
    path: Path

    @classmethod
    def create(cls, runs_dir: Path, film: str, now: datetime | None = None) -> RunLog:
        check_film_slug(film)
        stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
        film_dir = runs_dir / film
        film_dir.mkdir(parents=True, exist_ok=True)
        run_dir, n = film_dir / stamp, 1
        while True:
            try:
                run_dir.mkdir()
                break
            except FileExistsError:
                n += 1
                run_dir = film_dir / f"{stamp}-{n}"
        log = cls(run_dir / "run.jsonl")
        log.event("run_started", film=film)
        return log

    def event(self, kind: str, **fields: Any) -> None:
        line = {"event": kind, "at": datetime.now(UTC).isoformat(), **fields}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, default=_json_default) + "\n")

    def finish(self, total_usd: Decimal, stop_reason: str) -> None:
        self.event("run_finished", total_usd=total_usd, stop_reason=stop_reason)


def read_events(path: Path) -> list[dict[str, Any]]:
    """A Run Log's events, in order. A line cut short (a Run killed mid-write) or not an event is skipped."""
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events
