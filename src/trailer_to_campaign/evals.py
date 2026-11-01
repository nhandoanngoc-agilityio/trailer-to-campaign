"""The Eval Set: every Film in evals/eval_set.toml, scored against its Answer Key (ADR 0008).

    code = run_eval(prepare(Path("evals")), budget, runs_dir, lambda film: real_stages(film, settings, make_client))

Each Film gets one Eval Set Run, under one Film Budget and one Run Log, and the Films run
side by side (T19; --parallel caps how many at once):

1. its Planted Drafts and Clean Drafts go straight to the Checker, first, because
   that score is cheap and doesn't depend on what the Copywriter writes;
2. the Watcher, then the Copywriter and the Checker, run on its trailer, and every
   verdict the Checker gives is kept to score the input Traps.

No Approval is asked for or recorded: every Grounded Key Moment goes on to the
Copywriter, and the Run ends at the Checker. A Film whose folder is missing (Song
Lang is gitignored) is skipped and named in the report.

A run can skip what a change doesn't touch (T20): only_drafts stops after step 1, and
reused takes the Key Moments an earlier run saved, so no Watcher runs. Each report says
which it was, the commit, and a hash of the agents, so show_baseline can find a whole
run of `main`'s agents again instead of paying for a new one.

The Answer Key in evals/expected/ is human-written: this module only reads it,
checked strictly like film.toml, because a typo that dropped a Trap would make a
regression look like a pass.
"""

from __future__ import annotations

import json
import logging
import sys
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

import anyio

from trailer_to_campaign.agents.checker import Verdict
from trailer_to_campaign.agents.key_moments import KeyMoment, load_key_moments
from trailer_to_campaign.agents.loop import AgentFailed
from trailer_to_campaign.agents.post_text import nfc
from trailer_to_campaign.agents.review import CheckedPost, FilmBudgetReachedMidReview
from trailer_to_campaign.agents.social_posts import LANGUAGES, PLATFORM_LIMITS, SocialPost
from trailer_to_campaign.budget import FilmBudgetReached, FilmMeter
from trailer_to_campaign.catalog import load_film
from trailer_to_campaign.config import Settings
from trailer_to_campaign.eval_baseline import agents_hash, baselines, current_commit, main_agents_hash
from trailer_to_campaign.run_log import (
    EXIT_AGENT_FAILED,
    EXIT_FILM_BUDGET_REACHED,
    RunLog,
    check_film_slug,
    read_events,
)

log = logging.getLogger(__name__)

TRAPS = ("spoiler", "wrong_cast")
REPORTS = "_eval"  # under runs/: a Film slug has no "_", so this never collides with a Film's Run Logs

_DRAFT_KEYS = {"id", "platform", "language", "text"}
_KEYS = {
    "published_posts": {"platform", "language", "text", "url", "date"},
    "planted_drafts": _DRAFT_KEYS | {"trap", "spoiler_id", "wrong_name"},
    "clean_drafts": _DRAFT_KEYS,
    "input_traps": {"spoiler_id"},
}


class AnswerKeyError(ValueError):
    """The Eval Set or an Answer Key is missing, malformed, or names something the Film doesn't have."""


@dataclass(frozen=True)
class PublishedPost:
    platform: str  # any platform the studio posted on, shown beside the generated posts
    language: str
    text: str
    url: str
    date: str


@dataclass(frozen=True)
class PlantedDraft:
    id: str
    trap: str  # "spoiler" or "wrong_cast"
    platform: str
    language: str
    text: str
    spoiler_id: str | None = None  # a spoiler Trap: the Spoiler List item the Checker must name
    wrong_name: str | None = None  # a wrong_cast Trap: the name the Checker must point at


@dataclass(frozen=True)
class CleanDraft:
    id: str
    platform: str
    language: str
    text: str


@dataclass(frozen=True)
class AnswerKey:
    published_posts: list[PublishedPost]
    planted_drafts: list[PlantedDraft]
    clean_drafts: list[CleanDraft]
    input_traps: list[str]  # Spoiler List ids


@dataclass(frozen=True)
class EvalFilm:
    slug: str
    key: AnswerKey | None  # None: the Film's folder isn't on this clone, so it is skipped
    trailer: str = ""  # read once, here: the Run watches what prepare() checked


def prepare(eval_dir: Path) -> list[EvalFilm]:
    """Every Film in the Eval Set, each present one checked with its Answer Key, before anything is spent."""
    path = eval_dir / "eval_set.toml"
    try:
        slugs = tomllib.loads(path.read_text(encoding="utf-8"))["films"]
    except (OSError, tomllib.TOMLDecodeError, KeyError) as err:
        raise AnswerKeyError(f'{path} must list the Eval Set\'s Films: films = ["slug", ...]') from err
    if (
        not isinstance(slugs, list)
        or not slugs
        or not all(isinstance(s, str) for s in slugs)
        or len(set(slugs)) != len(slugs)
    ):
        raise AnswerKeyError(f"{path}: films must be a non-empty list of distinct slugs")
    films = []
    for slug in slugs:
        check_film_slug(slug)
        if not (eval_dir / "films" / slug).is_dir():
            films.append(EvalFilm(slug, None))
            continue
        film = load_film(eval_dir / "films", slug)
        if not film.catalog.trailer:
            raise AnswerKeyError(f"{slug} has no trailer link in its film.toml; the Eval Set watches the trailer")
        key = load_answer_key(eval_dir, slug, {s.id for s in film.spoilers})
        films.append(EvalFilm(slug, key, film.catalog.trailer))
    return films


def load_answer_key(eval_dir: Path, film: str, spoiler_ids: set[str]) -> AnswerKey:
    path = eval_dir / "expected" / f"{check_film_slug(film)}.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise AnswerKeyError(f"{film} is in the Eval Set but has no Answer Key at {path}") from err
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        raise AnswerKeyError(f"{path} could not be read: {err}") from err
    _only(data, set(_KEYS), path.name)
    tables = {name: _tables(data, name, path.name) for name in _KEYS}

    def listed(spoiler_id: str, where: str) -> str:
        if spoiler_id not in spoiler_ids:
            raise AnswerKeyError(f"{where} spoiler_id {spoiler_id!r} is not on {film}'s Spoiler List")
        return spoiler_id

    published = [
        PublishedPost(*(_text(t, k, where) for k in ("platform", "language", "text", "url")), _date(t, where))
        for where, t in tables["published_posts"]
    ]
    planted = []
    for where, t in tables["planted_drafts"]:
        trap = _text(t, "trap", where)
        if trap not in TRAPS:
            raise AnswerKeyError(f"{where} trap must be one of {', '.join(TRAPS)}, got {trap!r}")
        other = "wrong_name" if trap == "spoiler" else "spoiler_id"
        if other in t:  # it would be dropped unread: likely the wrong trap
            raise AnswerKeyError(f"{where}: a {trap} Trap has no {other}; is trap set to the right kind?")
        draft_id, platform, language, text = _draft(t, where)
        planted.append(
            PlantedDraft(
                draft_id,
                trap,
                platform,
                language,
                text,
                spoiler_id=listed(_text(t, "spoiler_id", where), where) if trap == "spoiler" else None,
                wrong_name=_text(t, "wrong_name", where) if trap == "wrong_cast" else None,
            )
        )
    clean = [CleanDraft(*_draft(t, where)) for where, t in tables["clean_drafts"]]
    traps = [listed(_text(t, "spoiler_id", where), where) for where, t in tables["input_traps"]]
    if twice := sorted({t for t in traps if traps.count(t) > 1}):
        raise AnswerKeyError(f"{path.name}: input Trap {', '.join(twice)} is listed twice")

    ids = [d.id for d in planted] + [d.id for d in clean]
    if twice := sorted({i for i in ids if ids.count(i) > 1}):
        raise AnswerKeyError(f"{path.name}: draft id {', '.join(twice)} is used twice")
    if not planted:
        raise AnswerKeyError(f"{path.name} needs at least one [[planted_drafts]] entry")
    return AnswerKey(published, planted, clean, traps)


def _only(table: dict[str, Any], allowed: set[str], where: str) -> None:
    if unknown := sorted(set(table) - allowed):
        raise AnswerKeyError(f"{where} has unknown keys {unknown}; allowed: {sorted(allowed)}")


def _tables(data: dict[str, Any], name: str, file: str) -> list[tuple[str, dict[str, Any]]]:
    entries = data.get(name, [])
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise AnswerKeyError(f"{file}: {name} must be [[{name}]] entries")
    out = []
    for i, entry in enumerate(entries, start=1):
        where = f"{file} {name} {i}"
        _only(entry, _KEYS[name], where)
        out.append((where, entry))
    return out


def _text(table: dict[str, Any], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AnswerKeyError(f"{where} needs {key} as non-empty text")
    return value.strip()


def _date(table: dict[str, Any], where: str) -> str:
    value = table.get("date")
    if isinstance(value, date):
        return value.isoformat()
    return _text(table, "date", where)


def _draft(table: dict[str, Any], where: str) -> tuple[str, str, str, str]:
    """A draft's id, platform, language and text: the Checker reviews it like a real post, so these must be real."""
    draft_id, platform, language, text = (_text(table, k, where) for k in ("id", "platform", "language", "text"))
    if platform not in PLATFORM_LIMITS:
        raise AnswerKeyError(f"{where} platform must be one of {', '.join(PLATFORM_LIMITS)}, got {platform!r}")
    if language not in LANGUAGES:
        raise AnswerKeyError(f"{where} language must be one of {', '.join(LANGUAGES)}, got {language!r}")
    limit, length = PLATFORM_LIMITS[platform], len(nfc(text))
    if length > limit:
        raise AnswerKeyError(f"{where}: {platform} posts are at most {limit} characters; this one is {length}")
    return draft_id, platform, language, text


# One Eval Set Run


@dataclass(frozen=True)
class EvalStages:
    check: Callable[[list[SocialPost], FilmMeter], Awaitable[list[Verdict]]]
    watch: Callable[[FilmMeter], Awaitable[list[KeyMoment]]]
    # Appends every verdict the Checker gives, rewrites included, to the list it is handed.
    write_and_check: Callable[[list[KeyMoment], FilmMeter, list[Verdict]], Awaitable[list[CheckedPost]]]


def real_stages(film: str, trailer: str, settings: Settings, make_client: Callable[[], Any]) -> EvalStages:
    """The real agents on the Eval Set's own films folder (settings.films_dir), each stage with its own client.

    trailer is the link prepare() read and checked, so film.toml is not read again here."""
    from trailer_to_campaign.agents import checker, review
    from trailer_to_campaign.pipeline import stage_client, watch_stage

    async def check(posts: list[SocialPost], meter: FilmMeter) -> list[Verdict]:
        async with stage_client(make_client) as client:
            servers = checker.checker_servers(settings)
            return await checker.check_posts(film, posts, servers=servers, client=client, meter=meter)

    watch = watch_stage(film, trailer, settings, make_client)

    async def write_and_check(moments: list[KeyMoment], meter: FilmMeter, seen: list[Verdict]) -> list[CheckedPost]:
        async with stage_client(make_client) as client:
            agents = review.agents_for(film, settings, client, meter)

            async def check_and_keep(posts: list[SocialPost]) -> list[Verdict]:
                verdicts = await agents.check(posts)
                seen.extend(verdicts)
                return verdicts

            return await review.write_and_check(moments, replace(agents, check=check_and_keep), meter)

    return EvalStages(check=check, watch=watch, write_and_check=write_and_check)


@dataclass
class FilmResult:
    film: str
    key: AnswerKey | None
    stop: str = "skipped"
    cost: Decimal = Decimal(0)
    caught: dict[str, bool] = field(default_factory=dict)  # Planted Draft id -> caught
    drafts_reviewed: bool = False  # did the Checker give verdicts on the Planted and Clean Drafts?
    false_alarms: list[str] = field(default_factory=list)  # Clean Draft ids the Checker rejected
    pipeline_verdicts: list[Verdict] = field(default_factory=list)
    posts: list[CheckedPost] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    run_log: Path | None = None
    key_moments: int = 0  # what the Watcher handed on
    drafts_only: bool = False  # --only drafts: no post was written, so no input Trap could be caught

    def input_caught(self, spoiler_id: str) -> bool:
        return any(
            p.kind == "spoiler" and p.spoiler_id == spoiler_id for v in self.pipeline_verdicts for p in v.problems
        )


def caught(draft: PlantedDraft, verdict: Verdict) -> bool:
    """A spoiler Trap is caught when the Checker names its Spoiler List item. A wrong name is caught when the
    Checker calls it an invented detail and points at it: a rejection for tone or clickbait whose quoted words
    happen to include the name never questioned it."""
    if verdict.passed:
        return False
    if draft.trap == "spoiler":
        return any(p.kind == "spoiler" and p.spoiler_id == draft.spoiler_id for p in verdict.problems)
    name = nfc(draft.wrong_name or "").casefold()
    return any(
        p.kind == "brand" and p.rule == "invented_detail" and name in nfc(p.offending_text).casefold()
        for p in verdict.problems
    )


@dataclass(frozen=True)
class ReusedKeyMoments:
    """Key Moments an earlier Eval Set run saved, so this one needs no Watcher."""

    source: Path  # that run's report folder
    commit: str | None
    by_film: dict[str, list[KeyMoment]]


def load_reused(report_dir: Path, films: list[EvalFilm]) -> ReusedKeyMoments:
    """Read every present Film's saved Key Moments, or raise ValueError naming what's missing, before any paid call.

    Each file is found from the report's own runs folder (<runs>/_eval/<time>/), so a moved folder still works."""
    try:
        summary = json.loads((report_dir / "summary.json").read_text(encoding="utf-8"))
        entries = {entry["film"]: entry for entry in summary["films"]}
    except (OSError, ValueError, KeyError, TypeError) as err:
        raise ValueError(f"{report_dir / 'summary.json'} could not be read as an Eval Set report") from err
    runs = report_dir.resolve().parents[1]
    by_film: dict[str, list[KeyMoment]] = {}
    for film in films:
        if film.key is None:
            continue
        parts = Path(str(entries.get(film.slug, {}).get("run_log"))).parts[-3:]
        if len(parts) != 3 or parts[0] != film.slug or ".." in parts:
            raise ValueError(f"{report_dir} has no Run of {film.slug} to reuse Key Moments from")
        try:
            by_film[film.slug] = load_key_moments(runs.joinpath(*parts).with_name("key_moments.json"))
        except ValueError as err:
            raise ValueError(f"{film.slug}: no Key Moments to reuse from {report_dir}: {err}") from err
    # A reused report's own commit never ran a Watcher: keep the commit of the one that did.
    return ReusedKeyMoments(report_dir, summary.get("key_moments_commit") or summary.get("commit"), by_film)


def _save_key_moments(path: Path, moments: list[KeyMoment]) -> None:
    """Next to the Run Log, for --key-moments-from. Failing to save costs only that later saving, never this Run."""
    try:
        path.write_text(json.dumps([asdict(m) for m in moments], ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as err:
        log.warning("could not save %s: %r", path, err)


async def evaluate_film(
    film: str,
    key: AnswerKey,
    budget: Decimal,
    runs_dir: Path,
    stages: EvalStages,
    started: dict[str, FilmMeter] | None = None,
    only_drafts: bool = False,
    reused: ReusedKeyMoments | None = None,
) -> FilmResult:
    """One Film's Eval Set Run. started, when given, gets the Film's meter, so a Run cut short can be accounted for."""
    run_log = RunLog.create(runs_dir, film)
    meter = FilmMeter(budget, run_log)
    if started is not None:
        started[film] = meter
    result = FilmResult(film, key, run_log=run_log.path, drafts_only=only_drafts)
    meter.event(
        "eval_run",
        planted_drafts=len(key.planted_drafts),
        clean_drafts=len(key.clean_drafts),
        input_traps=key.input_traps,
    )
    drafts: list[PlantedDraft | CleanDraft] = [*key.planted_drafts, *key.clean_drafts]
    posts = [SocialPost(d.platform, d.language, d.text, []) for d in drafts]
    stop = "completed"
    try:
        try:
            verdicts = await stages.check(posts, meter)
        except AgentFailed as err:  # fail closed: drafts the Checker couldn't review are not caught
            result.errors.append(f"the Checker could not review the drafts: {err}")
        else:
            result.drafts_reviewed = True
            for draft, verdict in zip(drafts, verdicts, strict=True):
                if isinstance(draft, PlantedDraft):
                    result.caught[draft.id] = caught(draft, verdict)
                elif not verdict.passed:
                    result.false_alarms.append(draft.id)
        if not only_drafts:
            if reused is not None:
                moments = reused.by_film[film]
                meter.event("key_moments_reused", source=str(reused.source), key_moments=len(moments))
            else:
                moments = await stages.watch(meter)
            result.key_moments = len(moments)
            if moments:
                _save_key_moments(run_log.path.with_name("key_moments.json"), moments)
                result.posts = await stages.write_and_check(moments, meter, result.pipeline_verdicts)
            else:
                result.errors.append("the Watcher found no Key Moments, so no posts were written")
    except FilmBudgetReached as err:
        stop = "film_budget_reached"
        result.errors.append(str(err))
        if isinstance(err, FilmBudgetReachedMidReview):
            result.posts = err.checked
    except AgentFailed as err:
        stop = "error"
        result.errors.append(str(err))
    except Exception as err:  # e.g. an API timeout: it ends this Film's Run, never the other Films' scores
        stop = "error"
        result.errors.append(f"{type(err).__name__}: {err}")
    except BaseException:
        run_log.finish(meter.spent, "error")  # Ctrl-C, a crash or a cancelled Film still leaves the total
        raise
    result.stop, result.cost = stop, meter.spent
    run_log.finish(meter.spent, stop)
    return result


def run_eval(
    films: list[EvalFilm],
    budget: Decimal,
    runs_dir: Path,
    make_stages: Callable[[EvalFilm], EvalStages],
    out: TextIO | None = None,
    watcher: str = "v0",
    parallel: int | None = None,
    only_drafts: bool = False,
    reused: ReusedKeyMoments | None = None,
) -> int:
    """One Eval Set Run per present Film, then the report. Returns 0 only if every Run completed cleanly.

    The Films run side by side in one event loop (T19), at most parallel at once (default: all), and the
    report keeps the Eval Set's order. On Ctrl-C every Film in flight is cancelled at its next step, so no new
    paid call starts, and the report is still written for the Films that finished, saying what it leaves out.
    A Film with no result never reads as a clean run, however it was stopped. watcher names the Watcher the
    stages run (v0 or sdk), so two reports can be compared."""
    if parallel is not None and parallel < 1:
        raise ValueError(f"parallel must be at least 1, got {parallel}")
    if only_drafts and reused is not None:
        raise ValueError("only_drafts writes no posts, so it has no use for reused Key Moments")
    # Worked out before any Film starts: what these Runs measure, and a failure here costs nothing.
    measured_on: dict[str, Any] = {
        "mode": "drafts" if only_drafts else "reused_key_moments" if reused else "full",
        "commit": current_commit(),
        "agents_hash": agents_hash(),
    }
    if reused is not None:
        measured_on |= {"key_moments_from": str(reused.source), "key_moments_commit": reused.commit}
    out = out or sys.stdout  # looked up now, not at import, so a redirected stdout is the one written to
    done: dict[str, FilmResult] = {}
    started: dict[str, FilmMeter] = {}
    present = [film for film in films if film.key is not None]
    for film in films:
        if film.key is None:
            done[film.slug] = FilmResult(film.slug, None)
            _say(f"{film.slug:20} skipped (not on this clone)", out)

    async def one(film: EvalFilm, slots: anyio.CapacityLimiter) -> None:
        assert film.key is not None
        async with slots:
            _say(f"{film.slug:20} running...", out)
            try:
                stages = make_stages(film)
                result = await evaluate_film(
                    film.slug, film.key, budget, runs_dir, stages, started, only_drafts=only_drafts, reused=reused
                )
            except Exception as err:  # its stages or Run Log failed: only this Film is lost, and what it spent is kept
                meter = started.get(film.slug)
                result = FilmResult(
                    film.slug,
                    film.key,
                    stop="error",
                    errors=[f"{type(err).__name__}: {err}"],
                    cost=meter.spent if meter else Decimal(0),
                    run_log=meter.run_log.path if meter else None,
                    drafts_only=only_drafts,
                )
            done[film.slug] = result  # first: it was paid for, whatever happens to its line
            _say(f"{film.slug:20} {_film_line(result)}", out)

    async def all_films() -> None:
        slots = anyio.CapacityLimiter(max(len(present), 1) if parallel is None else parallel)
        async with anyio.create_task_group() as group:
            for film in present:
                group.start_soon(one, film, slots)

    def report() -> list[FilmResult]:
        results = [done[film.slug] for film in films if film.slug in done]
        unfinished = [film.slug for film in present if film.slug not in done]
        extra = {**measured_on, **_unfinished(unfinished, started)}
        _write_report(results, runs_dir, out, extra, watcher=watcher)
        return results

    try:
        anyio.run(all_films)
    except BaseException as err:  # Ctrl-C: the Films already run were paid for, so their report is still written
        report()
        if isinstance(err, BaseExceptionGroup) and err.subgroup(KeyboardInterrupt):
            raise KeyboardInterrupt from err  # Ctrl-C inside one Film reads as Ctrl-C, not as a group
        raise
    results = report()
    if any(film.slug not in done for film in present):  # a stray cancellation ends the group without an error
        return EXIT_AGENT_FAILED

    ran = [r for r in results if r.key is not None]
    if any(r.stop == "film_budget_reached" for r in ran):
        return EXIT_FILM_BUDGET_REACHED
    if any(r.stop == "error" or r.errors for r in ran):
        return EXIT_AGENT_FAILED
    return 0


def _say(text: str, out: TextIO) -> None:
    """Progress for a person: a closed pipe (t2c eval | head) must not cost a Film, the report or how the run ends."""
    try:
        print(text, file=out, flush=True)
    except (OSError, ValueError) as err:  # ValueError: the stream is already closed
        log.warning("could not print a progress line: %r", err)


def _unfinished(unfinished: list[str], started: dict[str, FilmMeter]) -> dict[str, Any]:
    """What a report's totals leave out: Films cut short in flight, with their spend, and Films never begun."""
    if not unfinished:
        return {}
    in_flight = [film for film in unfinished if film in started]
    spent = sum((started[film].spent for film in in_flight), Decimal(0))
    fields: dict[str, Any] = {
        "interrupted": in_flight,
        "interrupted_spent_usd": str(spent),
        "interrupted_run_logs": {film: str(started[film].run_log.path) for film in in_flight},
    }
    not_started = [film for film in unfinished if film not in started]
    return {**fields, "not_started": not_started} if not_started else fields


def _write_report(
    results: list[FilmResult],
    runs_dir: Path,
    out: TextIO,
    extra: dict[str, Any],
    watcher: str = "v0",
) -> None:
    summary = {**_summary(results, watcher), **extra}
    report_dir = _report_dir(runs_dir)
    (report_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (report_dir / "report.md").write_text(_report(summary, results), encoding="utf-8")
    for line in [*_totals_lines(summary), *_unfinished_lines(summary)]:
        _say(line, out)
    _say(f"Report: {report_dir / 'report.md'}", out)


def _unfinished_lines(summary: dict[str, Any]) -> list[str]:
    in_flight, not_started = summary.get("interrupted", []), summary.get("not_started", [])
    if not in_flight and not not_started:
        return []
    lines = []
    if in_flight:
        spent = Decimal(summary["interrupted_spent_usd"])
        logs = ", ".join(summary["interrupted_run_logs"].values())
        lines.append(
            f"Interrupted during {', '.join(in_flight)}: ${spent:.4f} charged, not in the cost above "
            f"(a call cut off counts at its worst case). Run Logs: {logs}"
        )
    if not_started:
        lines.append(f"Never started: {', '.join(not_started)}.")
    return [*lines, "The totals cover only the Films that finished."]


def _film_line(r: FilmResult) -> str:
    s = _film_summary(r)
    line = (
        f"{r.stop:19} ${r.cost:.4f}  Planted Drafts {s['planted']['caught']}/{s['planted']['total']}  "
        f"false alarms {s['false_alarms']['count']}/{s['false_alarms']['total']}"
    )
    return line if r.drafts_only else f"{line}  input Traps {s['input_traps']['caught']}/{s['input_traps']['total']}"


def _film_summary(r: FilmResult) -> dict[str, Any]:
    if r.key is None:
        return {"film": r.film, "stop": "skipped"}
    # Fail closed both ways: a Planted Draft the Checker never reviewed is not caught, and a Clean Draft it never
    # reviewed is not clean, so it is left out of the false alarms rather than counted as a pass.
    planted = r.key.planted_drafts
    hit = {d.id: r.caught.get(d.id, False) for d in planted}
    input_traps = [] if r.drafts_only else r.key.input_traps  # not run, so neither caught nor missed
    by_trap = {
        trap: [sum(hit[d.id] for d in planted if d.trap == trap), sum(d.trap == trap for d in planted)]
        for trap in TRAPS
    }
    return {
        "film": r.film,
        "stop": r.stop,
        "cost_usd": str(r.cost),
        "planted": {
            "caught": sum(hit.values()),
            "total": len(planted),
            "by_trap": by_trap,
            "missed": [i for i, c in hit.items() if not c],
            "not_reviewed": 0 if r.drafts_reviewed else len(planted),
        },
        "false_alarms": {
            "count": len(r.false_alarms),
            "total": len(r.key.clean_drafts) if r.drafts_reviewed else 0,
            "ids": r.false_alarms,
        },
        "input_traps": {
            "caught": sum(r.input_caught(t) for t in input_traps),
            "total": len(input_traps),
            "not_caught": [t for t in input_traps if not r.input_caught(t)],
        },
        "watcher": _watcher_numbers(r),
        "errors": r.errors,
        "run_log": str(r.run_log),
    }


def _watcher_numbers(r: FilmResult) -> dict[str, Any]:
    """The Watcher's own share of a Film's Run, read from its Run Log: what v0 and the SDK Watcher are compared on."""
    # read_events skips a line cut short by Ctrl-C: the report must still be written.
    events = read_events(r.run_log) if r.run_log is not None and r.run_log.exists() else []
    mine = [e for e in events if e.get("agent") == "watcher"]
    calls = [e for e in mine if e["event"] == "model_call"]
    return {
        "cost_usd": str(sum((Decimal(e["cost_usd"]) for e in calls), Decimal(0))),
        "model_calls": len(calls),
        "tool_calls": sum(e["event"] == "tool_call" for e in mine),
        "grounding_rejections": sum(e["event"] == "key_moments_rejected" for e in mine),
        "key_moments": r.key_moments,
    }


def _summary(results: list[FilmResult], watcher: str = "v0") -> dict[str, Any]:
    films = [_film_summary(r) for r in results]
    ran = [f for f in films if f["stop"] != "skipped"]

    def total(part: str, a: str, b: str) -> dict[str, int]:
        return {a: sum(f[part][a] for f in ran), b: sum(f[part][b] for f in ran)}

    counts = ("model_calls", "tool_calls", "grounding_rejections", "key_moments")
    return {
        "at": datetime.now(UTC).isoformat(),
        "watcher": watcher,
        "films": films,
        "skipped": [f["film"] for f in films if f["stop"] == "skipped"],
        "totals": {
            "planted": {
                **total("planted", "caught", "total"),
                "not_reviewed": sum(f["planted"]["not_reviewed"] for f in ran),
            },
            "false_alarms": total("false_alarms", "count", "total"),
            "input_traps": total("input_traps", "caught", "total"),
            "cost_usd": str(sum((Decimal(f["cost_usd"]) for f in ran), Decimal(0))),
            "watcher": {
                "cost_usd": str(sum((Decimal(f["watcher"]["cost_usd"]) for f in ran), Decimal(0))),
                **{n: sum(f["watcher"][n] for f in ran) for n in counts},
            },
        },
    }


def _totals_lines(summary: dict[str, Any]) -> list[str]:
    t = summary["totals"]
    ran = len(summary["films"]) - len(summary["skipped"])
    unreviewed = t["planted"]["not_reviewed"]
    mode = summary.get("mode", "full")
    if mode == "drafts":
        input_traps, watcher = "Input Traps: not run (--only drafts)", "Watcher: not run (--only drafts)"
    else:
        input_traps = f"Input Traps: caught {t['input_traps']['caught']} of {t['input_traps']['total']}"
        watcher = (
            f"Watcher: {summary['watcher']}, ${Decimal(t['watcher']['cost_usd']):.4f}, "
            f"{t['watcher']['grounding_rejections']} grounding rejections, {t['watcher']['key_moments']} Key Moments"
        )
    if mode == "reused_key_moments":
        commit = (summary.get("key_moments_commit") or "unknown")[:7]
        watcher = f"Watcher: not run, Key Moments reused from {summary['key_moments_from']} (commit {commit})"
    return [
        f"Planted Drafts: caught {t['planted']['caught']} of {t['planted']['total']}"
        + (f" ({unreviewed} not reviewed: the Checker failed on them)" if unreviewed else ""),
        input_traps,
        f"Clean Drafts: false alarms {t['false_alarms']['count']} of {t['false_alarms']['total']}",
        f"Cost: ${Decimal(t['cost_usd']):.4f} over {ran} Films"
        + (f" ({len(summary['skipped'])} skipped)" if summary["skipped"] else ""),
        watcher,
    ]


def show_baseline(
    runs_dir: Path,
    watcher: str,
    out: TextIO | None = None,
    main_hash: Callable[[], str | None] = main_agents_hash,
    present: list[str] | None = None,
) -> int:
    """Print the newest saved report of `main`'s agents, or say a new run is needed. Free: no model is called.

    present is the Films on this clone, to warn when the Baseline ran others. Returns 0 if there is one to
    compare with, and 1 if not: without `main` to read, this checkout's own run must not pass for its numbers."""
    out = out or sys.stdout
    wanted = main_hash()
    if wanted is None:
        _say("git could not read main (origin/main or main), so there is nothing to compare with.", out)
        return EXIT_AGENT_FAILED
    reports = runs_dir / REPORTS
    for path, summary in baselines(reports, wanted, watcher):
        try:
            lines = _baseline_lines(path, summary, present)
        except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError) as err:
            log.debug("passing over %s: %r", path, err)  # an older shape, or a hand-trimmed report
            continue
        for line in lines:
            _say(line, out)
        return 0
    _say(
        f"No whole report in {reports} matches main's agents on the {watcher} Watcher: a new run on main is needed. "
        "A run with uncommitted or untracked files under the agents' folders has a hash of its own and never matches.",
        out,
    )
    return EXIT_AGENT_FAILED


def _baseline_lines(path: Path, summary: dict[str, Any], present: list[str] | None) -> list[str]:
    commit = (summary.get("commit") or "unknown")[:7]
    lines = [f"Baseline for main's agents: {path} (commit {commit}, run at {summary['at']})"]
    ran = [film for film in summary["films"] if film["stop"] != "skipped"]
    for film in ran:
        planted = film["planted"]
        lines.append(
            f"{film['film']:20} ${Decimal(film['cost_usd']):.4f}  Planted Drafts {planted['caught']}/{planted['total']}"
        )
    lines += _totals_lines(summary)
    t = summary["totals"]
    without_watcher = Decimal(t["cost_usd"]) - Decimal(t["watcher"]["cost_usd"])
    lines.append(f"Without the Watcher: ${without_watcher:.4f}, to compare with a --key-moments-from run")
    names = {film["film"] for film in ran}
    if present is not None and names != set(present):
        lines.append(
            f"Films differ: this Baseline ran {', '.join(sorted(names))}; this clone has {', '.join(sorted(present))}. "
            "Compare Film by Film, not the totals."
        )
    return lines


def _report_dir(runs_dir: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = runs_dir / REPORTS
    base.mkdir(parents=True, exist_ok=True)
    path, n = base / stamp, 1
    while True:
        try:
            path.mkdir()
            return path
        except FileExistsError:
            n += 1
            path = base / f"{stamp}-{n}"


def _report(summary: dict[str, Any], results: list[FilmResult]) -> str:
    """For a person: the numbers, then per Film what the Checker missed and the posts beside the published ones."""
    lines = ["# Eval Set report", "", f"Run at {summary['at']}.", ""]
    lines += [f"- {line}" for line in [*_totals_lines(summary), *_unfinished_lines(summary)]]
    for r, s in zip(results, summary["films"], strict=True):
        lines += ["", f"## {r.film}", ""]
        if r.key is None:
            lines.append("Skipped: the Film's folder is not on this clone.")
            continue
        lines += [f"- {_film_line(r)}", f"- Run Log: `{r.run_log}`"]
        lines += [f"- Error: {e}" for e in r.errors]
        missed = [d for d in r.key.planted_drafts if not r.caught.get(d.id, False)]
        if missed:
            lines += ["", "### Planted Drafts the Checker missed", ""]
            lines += [f"- `{d.id}` ({d.trap}): {d.text}" for d in missed]
        if r.false_alarms:
            lines += ["", "### Clean Drafts rejected (false alarms)", ""]
            lines += [f"- `{d.id}`: {d.text}" for d in r.key.clean_drafts if d.id in r.false_alarms]
        if s["input_traps"]["not_caught"]:
            lines += [
                "",
                "### Input Traps not caught",
                "",
                "No verdict named these. Read the posts below: either none touched the Trap, or the Checker missed it.",
                "",
            ]
            lines += [f"- `{t}`" for t in s["input_traps"]["not_caught"]]
        if r.drafts_only:
            continue
        lines += ["", "### Generated posts", ""]
        lines += [f"- **{c.status}** {c.post.platform} {c.post.language}: {c.post.text}" for c in r.posts] or [
            "- (none)"
        ]
        lines += ["", "### Published posts", ""]
        lines += [f"- {p.platform} {p.language}, {p.date} ({p.url}): {p.text}" for p in r.key.published_posts] or [
            "- (none in the Answer Key)"
        ]
    return "\n".join(lines) + "\n"
