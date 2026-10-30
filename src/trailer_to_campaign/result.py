"""The side-by-side result (T12): Time Saved, Approval Rate and cost per Film for one real launch.

    result = film_result(film, runs_dir, films_dir, old_minutes=240, new_minutes=60)
    page = write_result(result, runs_dir)          # runs/<film>/result.md and result.json

One marketer prepares the Film's posts the old way and another uses Trailer-to-Campaign,
each timing their own active work with a stopwatch (docs/side-by-side.md). Those two
times give Time Saved. Everything else comes from the Film's own Run Logs:
- cost per Film is the sum of every Run, re-runs and failures included, since the Film
  Budget caps each Run, not the Film; a Run killed mid-way counts what its model calls spent;
- the Approval Rate is the Run whose Campaign was used: the last completed one with Approval;
- each Run's Run Log time (start to finish) is shown beside the stopwatch, as a check only:
  it includes any wait for the Marketer at Approval.

The page holds the Film's title and numbers only, never a post, a Key Moment or the
Spoiler List, so it can be shared once the team agrees. It sits next to the Run Logs,
gitignored, until then.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trailer_to_campaign.catalog import load_film
from trailer_to_campaign.run_log import check_film_slug

UNFINISHED = "did not finish"


@dataclass(frozen=True)
class RunSummary:
    stamp: str  # the Run's folder under runs/<film>/
    ended: str  # completed, film_budget_reached, error, or "did not finish"
    cost: Decimal
    minutes: Decimal | None  # Run Log time, start to finish; None if it never finished
    approval: dict[str, Any] | None  # its approval_rate event, if it reached one


@dataclass(frozen=True)
class FilmResult:
    film: str
    title: str
    old_minutes: Decimal
    new_minutes: Decimal
    runs: list[RunSummary] = field(default_factory=list)

    @property
    def cost(self) -> Decimal:
        return sum((r.cost for r in self.runs), Decimal(0))

    @property
    def used(self) -> RunSummary | None:
        """The Run whose Campaign was used: the last completed one with Approval."""
        done = [r for r in self.runs if r.ended == "completed" and r.approval is not None]
        return done[-1] if done else None

    @property
    def approval(self) -> dict[str, Any]:
        return self.used.approval if self.used and self.used.approval else {}

    @property
    def time_saved(self) -> Decimal:
        return self.old_minutes - self.new_minutes

    @property
    def time_saved_share(self) -> Decimal:
        return self.time_saved / self.old_minutes

    @property
    def run_log_minutes(self) -> Decimal:
        return sum((r.minutes for r in self.runs if r.minutes is not None), Decimal(0))


def film_result(
    film: str, runs_dir: Path, films_dir: Path, *, old_minutes: float | Decimal, new_minutes: float | Decimal
) -> FilmResult:
    """Read every Run Log of the Film. Refuses bad input before anything is written."""
    check_film_slug(film)
    old, new = _minutes(old_minutes, "old-way"), _minutes(new_minutes, "new-way")
    title = load_film(films_dir, film).catalog.title
    logs = sorted((runs_dir / film).glob("*/run.jsonl"))
    if not logs:
        raise ValueError(f"no Run Logs for {film} under {runs_dir.name}/{film}/: run the Film first")
    return FilmResult(film, title, old, new, [_summary(log) for log in logs])


def write_result(result: FilmResult, runs_dir: Path) -> Path:
    folder = runs_dir / result.film
    page = folder / "result.md"
    page.write_text(_page(result), encoding="utf-8")
    (folder / "result.json").write_text(json.dumps(_numbers(result), indent=2, ensure_ascii=False), encoding="utf-8")
    return page


def _minutes(value: float | Decimal, which: str) -> Decimal:
    try:
        minutes = Decimal(str(value))
    except ArithmeticError:
        minutes = Decimal(0)
    if not minutes.is_finite() or minutes <= 0:
        raise ValueError(f"--{which} must be a positive number of minutes, got {value!r}")
    return minutes


def _summary(log: Path) -> RunSummary:
    events = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:  # a line cut short when the Run was killed: the rest still counts
            continue
    started = next((e for e in events if e.get("event") == "run_started"), None)
    finished = next((e for e in reversed(events) if e.get("event") == "run_finished"), None)
    approval = next((e for e in reversed(events) if e.get("event") == "approval_rate"), None)
    if finished is not None:
        cost, ended = Decimal(finished["total_usd"]), finished.get("stop_reason") or UNFINISHED
    else:  # killed mid-Run: what its model calls spent was still spent
        cost = sum((Decimal(e["cost_usd"]) for e in events if e.get("event") == "model_call"), Decimal(0))
        ended = UNFINISHED
    minutes = None
    if started is not None and finished is not None:
        seconds = (datetime.fromisoformat(finished["at"]) - datetime.fromisoformat(started["at"])).total_seconds()
        minutes = Decimal(str(round(seconds / 60, 1)))
    return RunSummary(log.parent.name, ended, cost, minutes, approval)


def _rate(fraction: str) -> str:
    """'13/15' -> '13/15 (87%)'."""
    try:
        done, total = (int(n) for n in fraction.split("/"))
    except (ValueError, AttributeError):
        return str(fraction)
    return f"{fraction} ({done / total:.0%})" if total else fraction


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _page(r: FilmResult) -> str:
    a = r.approval
    lines = [
        f"# {r.title}: side by side",
        "",
        f"- Time Saved: {r.time_saved:g} min ({r.time_saved_share:.0%})",
        f"- Approval Rate: {_rate(a['overall'])}"
        if a
        else "- Approval Rate: not finished (no completed Run with Approval)",
        f"- Cost per Film: ${r.cost:.4f} over {_plural(len(r.runs), 'Run')}",
        "",
        "## How it was measured",
        "",
        f"- The old way: {r.old_minutes:g} min of active work, timed by that marketer's stopwatch.",
        f"- Trailer-to-Campaign: {r.new_minutes:g} min of active work, timed the same way.",
        "- Both started with the trailer link and the catalog facts, and stopped with the six Social Posts ready "
        "to publish (docs/side-by-side.md).",
        f"- The Run Logs add up to {r.run_log_minutes:g} min from start to finish. That includes any wait for the "
        "Marketer at Approval, so it is a check on the stopwatch, not the measure.",
        "- Cost per Film is every Run's cost: the Film Budget caps each Run, not the Film, so re-runs and failed "
        "Runs count.",
    ]
    if a:
        light, heavy = a.get("light_edit_change") or {}, a.get("heavy_edit_change") or {}
        flagged = a.get("light_edits_over_flag", a.get("light_edits_over_30pct")) or []
        lines += [
            "",
            f"## Approval Rate, from the Run that was used ({r.used.stamp if r.used else ''})",
            "",
            f"- Key Moments: {_rate(a.get('key_moments', '?'))}",
            f"- Social Posts: {_rate(a.get('social_posts', '?'))}",
            f"- Light Edits: {light.get('count', 0)}, mean measured change {_share(light.get('mean'))}; "
            f"heavy edits: {heavy.get('count', 0)}, mean {_share(heavy.get('mean'))}",
            f"- Light Edits that changed over {_share(a.get('light_edit_flag', 0.3))} of the words: {len(flagged)}",
        ]
    lines += ["", "## Runs", "", "| Run | Ended | Cost | Run Log time |", "|---|---|---|---|"]
    lines += [
        f"| {s.stamp} | {s.ended} | ${s.cost:.4f} | {f'{s.minutes:g} min' if s.minutes is not None else '-'} |"
        for s in r.runs
    ]
    return "\n".join(lines) + "\n"


def _share(value: Any) -> str:
    return "-" if value is None else f"{float(value):.0%}"


def _numbers(r: FilmResult) -> dict[str, Any]:
    return {
        "film": r.film,
        "title": r.title,
        "old_way_minutes": str(r.old_minutes),
        "new_way_minutes": str(r.new_minutes),
        "time_saved_minutes": str(r.time_saved),
        "approval_rate": r.approval.get("overall"),
        "cost_usd": str(r.cost),
        "run_log_minutes": str(r.run_log_minutes),
        "runs": [
            {
                "run": s.stamp,
                "ended": s.ended,
                "cost_usd": str(s.cost),
                "minutes": None if s.minutes is None else str(s.minutes),
            }
            for s in r.runs
        ],
        "used_run": r.used.stamp if r.used else None,
    }
