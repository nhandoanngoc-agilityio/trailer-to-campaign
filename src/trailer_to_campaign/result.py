"""The side-by-side result (T12): Time Saved, Approval Rate and cost per Film for one real launch.

    result = film_result(film, runs_dir, films_dir, old_minutes=240, new_minutes=60, old_way_source="stopwatch")
    page = write_result(result, runs_dir)          # runs/<film>/result.md and result.json

One marketer prepares the Film's posts the old way and another uses Trailer-to-Campaign,
each timing their own active work with a stopwatch (docs/side-by-side.md). Those two
times give Time Saved. The old way's source is always given, never assumed (T28): a time
from memory makes Time Saved an estimate, and the page says so. Everything else comes from
the Film's own Run Logs:
- cost per Film is the sum of every launch Run, re-runs and failures included, since the Film
  Budget caps each Run, not the Film; a Run killed mid-way counts what its model calls spent.
  Eval Set and dummy Runs of the Film are left out: they are not launch work;
- the Approval Rate is the Run whose Campaign was used: by default the last Run to finish that
  gave a Campaign (completed, at least one Social Post approved), or the one named with --run;
- each Run's Run Log time (start to finish) is shown beside the two times, as a check only:
  it includes any wait for the Marketer at Approval.

The page holds the Film's title and numbers only, never a post, a Key Moment or the
Spoiler List, so it can be shared once the team agrees. It sits next to the Run Logs,
gitignored, until then.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from trailer_to_campaign.catalog import load_film
from trailer_to_campaign.pipeline import LIGHT_EDIT_FLAG
from trailer_to_campaign.run_log import check_film_slug, read_events

UNFINISHED = "did not finish"
OLD_WAY_SOURCES = ("stopwatch", "memory")  # cli.py offers these


@dataclass(frozen=True)
class RunSummary:
    stamp: str  # the Run's folder under runs/<film>/
    ended: str  # completed, film_budget_reached, error, or "did not finish"
    cost: Decimal
    started: datetime | None
    finished: datetime | None
    approval: dict[str, Any] | None  # its approval_rate event, if it reached one
    launch: bool = True  # False for an Eval Set or dummy Run: not launch work, so not counted

    @property
    def minutes(self) -> Decimal | None:
        """Run Log time, start to finish; None if it never finished."""
        if self.started is None or self.finished is None:
            return None
        return Decimal(str(round((self.finished - self.started).total_seconds() / 60, 1)))

    @property
    def gave_a_campaign(self) -> bool:
        """Completed, with Approval, and at least one Social Post approved: a Campaign came out of it."""
        posts = (self.approval or {}).get("social_posts", "0/0")
        return self.ended == "completed" and self.approval is not None and _done(posts) > 0


@dataclass(frozen=True)
class FilmResult:
    film: str
    title: str
    old_minutes: Decimal
    new_minutes: Decimal
    old_way_source: str  # one of OLD_WAY_SOURCES
    runs: list[RunSummary] = field(default_factory=list)  # launch Runs, in the order they started
    used: RunSummary | None = None  # the Run whose Campaign was used
    left_out: int = 0  # Eval Set and dummy Runs of the Film, not counted

    def __post_init__(self) -> None:
        """Refused here, so nothing can reach the page with a source it would read as a stopwatch."""
        if self.old_way_source not in OLD_WAY_SOURCES:
            raise ValueError(f"--old-way-source must be stopwatch or memory, got {self.old_way_source!r}")

    @property
    def cost(self) -> Decimal:
        return sum((r.cost for r in self.runs), Decimal(0))

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
    def time_saved_is_estimate(self) -> bool:
        return self.old_way_source == "memory"

    @property
    def run_log_minutes(self) -> Decimal:
        return sum((r.minutes for r in self.runs if r.minutes is not None), Decimal(0))


Minutes = str | float | Decimal  # from the command line, minutes arrive as text


def film_result(
    film: str,
    runs_dir: Path,
    films_dir: Path,
    *,
    old_minutes: Minutes,
    new_minutes: Minutes,
    old_way_source: str,
    used_run: str | None = None,
) -> FilmResult:
    """Read every Run Log of the Film. Refuses bad input before anything is written.

    used_run names the Run whose Campaign was used; by default it is the last Run to finish
    that gave a Campaign (completed, with at least one Social Post approved)."""
    check_film_slug(film)
    old, new = _minutes(old_minutes, "old-way"), _minutes(new_minutes, "new-way")
    title = load_film(films_dir, film).catalog.title
    every = [_summary(log) for log in (runs_dir / film).glob("*/run.jsonl")]
    runs = sorted((r for r in every if r.launch), key=_started_order)
    if not runs:
        raise ValueError(f"no Run Logs for {film} under {runs_dir.name}/{film}/: run the Film first")
    return FilmResult(film, title, old, new, old_way_source, runs, _used(runs, used_run), len(every) - len(runs))


def _used(runs: list[RunSummary], named: str | None) -> RunSummary | None:
    if named is not None:
        match = [r for r in runs if r.stamp == named and r.ended == "completed" and r.approval is not None]
        if not match:
            raise ValueError(f"no completed Run with Approval named {named!r}; see the Runs under runs/<film>/")
        return match[0]
    campaigns = [r for r in runs if r.gave_a_campaign]
    return max(campaigns, key=lambda r: r.finished or datetime.min) if campaigns else None


def _started_order(r: RunSummary) -> tuple[Any, ...]:
    """By start time; Runs started in the same second by their folder's suffix (-2 before -10)."""
    base, _, n = r.stamp.partition("-")
    return (r.started is None, r.started or datetime.min, base, int(n) if n.isdigit() else 0)


def write_result(result: FilmResult, runs_dir: Path) -> Path:
    folder = runs_dir / result.film
    page = folder / "result.md"
    page.write_text(_page(result), encoding="utf-8")
    (folder / "result.json").write_text(json.dumps(_numbers(result), indent=2, ensure_ascii=False), encoding="utf-8")
    return page


def _minutes(value: Minutes, which: str) -> Decimal:
    try:
        minutes = Decimal(str(value))
    except ArithmeticError:
        minutes = Decimal(0)
    if not minutes.is_finite() or minutes <= 0:
        raise ValueError(f"--{which} must be a positive number of minutes, got {value!r}")
    return minutes


def _summary(log: Path) -> RunSummary:
    stamp = log.parent.name
    events = read_events(log)  # a line cut short when the Run was killed is skipped: the rest still counts
    started = next((e for e in events if e.get("event") == "run_started"), None)
    finished = next((e for e in reversed(events) if e.get("event") == "run_finished"), None)
    approval = next((e for e in reversed(events) if e.get("event") == "approval_rate"), None)
    launch = not any(e.get("event") == "eval_run" or e.get("agent") == "dummy" for e in events)
    if finished is not None:
        cost, ended = _money(finished.get("total_usd"), stamp), finished.get("stop_reason") or UNFINISHED
    else:  # killed mid-Run: what its model calls spent was still spent
        calls = [e for e in events if e.get("event") == "model_call"]
        cost = sum((_money(e.get("cost_usd"), stamp) for e in calls), Decimal(0))
        ended = UNFINISHED
    return RunSummary(stamp, ended, cost, _when(started, stamp), _when(finished, stamp), approval, launch)


def _money(value: Any, stamp: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        amount = Decimal("NaN")
    if value is None or not amount.is_finite():
        raise ValueError(f"the Run Log {stamp}/run.jsonl has a cost that isn't an amount: {value!r}")
    return amount


def _when(event: dict[str, Any] | None, stamp: str) -> datetime | None:
    if event is None:
        return None
    try:
        return datetime.fromisoformat(str(event.get("at")))
    except ValueError:
        raise ValueError(f"the Run Log {stamp}/run.jsonl has a time that can't be read: {event.get('at')!r}") from None


def _done(fraction: str) -> int:
    try:
        return int(str(fraction).split("/")[0])
    except ValueError:
        return 0


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
    label = "Time Saved (estimate)" if r.time_saved_is_estimate else "Time Saved"
    if r.time_saved >= 0:
        saved = f"- {label}: {r.time_saved:g} min ({r.time_saved_share:.0%})"
    else:
        saved = f"- {label}: none: Trailer-to-Campaign took {-r.time_saved:g} min longer ({-r.time_saved_share:.0%})"
    lines = [
        f"# {r.title}: side by side",
        "",
        saved,
        f"- Approval Rate: {_rate(a['overall'])}"
        if a
        else "- Approval Rate: not finished (no completed Run that gave a Campaign)",
        f"- Cost per Film: ${r.cost:.4f} over {_plural(len(r.runs), 'Run')}",
        "",
        *_how_the_times_were_got(r),
        "- Cost per Film is every Run's cost: the Film Budget caps each Run, not the Film, so re-runs and failed "
        "Runs count."
        + (
            f" {_plural(r.left_out, 'Eval Set or dummy Run')} {'is' if r.left_out == 1 else 'are'} left out: "
            "not launch work."
            if r.left_out
            else ""
        ),
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
            f"- Light Edits that changed over {_share(a.get('light_edit_flag', LIGHT_EDIT_FLAG))} of the words: "
            f"{len(flagged)}",
            f"- Social Posts approved over a flag: {_count_or_unknown(a, 'approved_over_a_flag')}; "
            f"approved with no re-check: {_count_or_unknown(a, 'approved_not_rechecked')}",
        ]
    lines += ["", "## Runs", "", "| Run | Ended | Cost | Run Log time |", "|---|---|---|---|"]
    lines += [
        f"| {s.stamp} | {s.ended} | ${s.cost:.4f} | {f'{s.minutes:g} min' if s.minutes is not None else '-'} |"
        for s in r.runs
    ]
    return "\n".join(lines) + "\n"


def _how_the_times_were_got(r: FilmResult) -> list[str]:
    """Only what --old-way-source says happened. It says nothing of how the new way was timed."""
    run_logs = (
        f"- The Run Logs add up to {r.run_log_minutes:g} min from start to finish. That includes any wait for the "
        "Marketer at Approval, so it is a check on "
    )
    if r.old_way_source == "memory":
        return [
            "## Where the numbers came from",
            "",
            f"- The old way: {r.old_minutes:g} min, from memory, so Time Saved is an estimate, not a measurement.",
            f"- Trailer-to-Campaign: {r.new_minutes:g} min of active work, as that marketer gave it.",
            "- The protocol has both start with the trailer link and the catalog facts, and stop with the six "
            "Social Posts ready to publish (docs/side-by-side.md). A time from memory can't show the old way "
            "kept to it.",
            run_logs + "Trailer-to-Campaign's time only. Nothing here checks the old way's.",
        ]
    return [
        "## How it was measured",
        "",
        f"- The old way: {r.old_minutes:g} min of active work, timed by that marketer's stopwatch.",
        f"- Trailer-to-Campaign: {r.new_minutes:g} min of active work, timed the same way.",
        "- Both started with the trailer link and the catalog facts, and stopped with the six Social Posts ready "
        "to publish (docs/side-by-side.md).",
        run_logs + "the stopwatch, not the measure.",
    ]


def _flag_count(approval: dict[str, Any], key: str) -> int | None:
    """How many posts, or None for a Run from before T22 (or none used): unknown is not zero."""
    names = approval.get(key)
    return len(names) if isinstance(names, list) else None


def _count_or_unknown(approval: dict[str, Any], key: str) -> str:
    count = _flag_count(approval, key)
    return "not recorded" if count is None else str(count)


def _share(value: Any) -> str:
    return "-" if value is None else f"{float(value):.0%}"


def _numbers(r: FilmResult) -> dict[str, Any]:
    return {
        "film": r.film,
        "title": r.title,
        "old_way_minutes": str(r.old_minutes),
        "new_way_minutes": str(r.new_minutes),
        "old_way_source": r.old_way_source,
        "time_saved_is_estimate": r.time_saved_is_estimate,
        "time_saved_minutes": str(r.time_saved),
        "approval_rate": r.approval.get("overall"),
        "approved_over_a_flag": _flag_count(r.approval, "approved_over_a_flag"),
        "approved_not_rechecked": _flag_count(r.approval, "approved_not_rechecked"),
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
