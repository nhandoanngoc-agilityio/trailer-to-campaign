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

The Answer Key in evals/expected/ is human-written: this module only reads it,
checked strictly like film.toml, because a typo that dropped a Trap would make a
regression look like a pass.
"""

from __future__ import annotations

import json
import sys
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

import anyio

from trailer_to_campaign.agents.checker import Verdict
from trailer_to_campaign.agents.key_moments import KeyMoment
from trailer_to_campaign.agents.loop import AgentFailed
from trailer_to_campaign.agents.post_text import nfc
from trailer_to_campaign.agents.review import CheckedPost, FilmBudgetReachedMidReview
from trailer_to_campaign.agents.social_posts import LANGUAGES, PLATFORM_LIMITS, SocialPost
from trailer_to_campaign.budget import FilmBudgetReached, FilmMeter
from trailer_to_campaign.catalog import load_film
from trailer_to_campaign.config import Settings
from trailer_to_campaign.run_log import (
    EXIT_AGENT_FAILED,
    EXIT_FILM_BUDGET_REACHED,
    RunLog,
    check_film_slug,
    read_events,
)

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


async def evaluate_film(film: str, key: AnswerKey, budget: Decimal, runs_dir: Path, stages: EvalStages) -> FilmResult:
    run_log = RunLog.create(runs_dir, film)
    meter = FilmMeter(budget, run_log)
    result = FilmResult(film, key, run_log=run_log.path)
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
        moments = await stages.watch(meter)
        result.key_moments = len(moments)
        if moments:
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
) -> int:
    """One Eval Set Run per present Film, then the report. Returns 0 only if every Run completed cleanly.

    The Films run side by side in one event loop (T19), at most parallel at once (default: all), and the
    report keeps the Eval Set's order. On Ctrl-C every Film in flight is cancelled, so no paid call goes on,
    and the report is still written for the Films that finished. watcher names the Watcher the stages run
    (v0 or sdk), so two reports can be compared."""
    out = out or sys.stdout  # looked up now, not at import, so a redirected stdout is the one written to
    done: dict[str, FilmResult] = {}
    present = [film for film in films if film.key is not None]
    for film in films:
        if film.key is None:
            done[film.slug] = FilmResult(film.slug, None)
            print(f"{film.slug:20} skipped (not on this clone)", file=out, flush=True)

    async def one(film: EvalFilm, slots: anyio.CapacityLimiter) -> None:
        assert film.key is not None
        async with slots:
            print(f"{film.slug:20} running...", file=out, flush=True)
            try:
                result = await evaluate_film(film.slug, film.key, budget, runs_dir, make_stages(film))
            except Exception as err:  # the Run couldn't start (its stages or Run Log): only this Film is lost
                result = FilmResult(film.slug, film.key, stop="error", errors=[f"{type(err).__name__}: {err}"])
            print(f"{film.slug:20} {_film_line(result)}", file=out, flush=True)
            done[film.slug] = result

    async def all_films() -> None:
        slots = anyio.CapacityLimiter(parallel or max(len(present), 1))
        async with anyio.create_task_group() as group:
            for film in present:
                group.start_soon(one, film, slots)

    def in_order() -> list[FilmResult]:
        return [done[film.slug] for film in films if film.slug in done]

    try:
        anyio.run(all_films)
    except BaseException as err:  # Ctrl-C: the Films already run were paid for, so their report is still written
        unfinished = [film.slug for film in present if film.slug not in done]
        _write_report(in_order(), runs_dir, out, interrupted=unfinished, watcher=watcher)
        if isinstance(err, BaseExceptionGroup) and err.subgroup(KeyboardInterrupt):
            raise KeyboardInterrupt from err  # Ctrl-C inside one Film reads as Ctrl-C, not as a group
        raise
    results = in_order()
    _write_report(results, runs_dir, out, watcher=watcher)

    ran = [r for r in results if r.key is not None]
    if any(r.stop == "film_budget_reached" for r in ran):
        return EXIT_FILM_BUDGET_REACHED
    if any(r.stop == "error" or r.errors for r in ran):
        return EXIT_AGENT_FAILED
    return 0


def _write_report(
    results: list[FilmResult],
    runs_dir: Path,
    out: TextIO,
    interrupted: list[str] | None = None,
    watcher: str = "v0",
) -> None:
    summary = _summary(results, watcher)
    if interrupted:
        summary["interrupted"] = interrupted  # the Films in flight or not started: not in the totals
    report_dir = _report_dir(runs_dir)
    (report_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (report_dir / "report.md").write_text(_report(summary, results), encoding="utf-8")
    for line in _totals_lines(summary):
        print(line, file=out)
    if interrupted:
        print(f"Interrupted during {', '.join(interrupted)}: the totals cover only the Films that finished.", file=out)
    print(f"Report: {report_dir / 'report.md'}", file=out)


def _film_line(r: FilmResult) -> str:
    s = _film_summary(r)
    return (
        f"{r.stop:19} ${r.cost:.4f}  Planted Drafts {s['planted']['caught']}/{s['planted']['total']}  "
        f"false alarms {s['false_alarms']['count']}/{s['false_alarms']['total']}  "
        f"input Traps {s['input_traps']['caught']}/{s['input_traps']['total']}"
    )


def _film_summary(r: FilmResult) -> dict[str, Any]:
    if r.key is None:
        return {"film": r.film, "stop": "skipped"}
    # Fail closed both ways: a Planted Draft the Checker never reviewed is not caught, and a Clean Draft it never
    # reviewed is not clean, so it is left out of the false alarms rather than counted as a pass.
    planted = r.key.planted_drafts
    hit = {d.id: r.caught.get(d.id, False) for d in planted}
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
            "caught": sum(r.input_caught(t) for t in r.key.input_traps),
            "total": len(r.key.input_traps),
            "not_caught": [t for t in r.key.input_traps if not r.input_caught(t)],
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
    return [
        f"Planted Drafts: caught {t['planted']['caught']} of {t['planted']['total']}"
        + (f" ({unreviewed} not reviewed: the Checker failed on them)" if unreviewed else ""),
        f"Input Traps: caught {t['input_traps']['caught']} of {t['input_traps']['total']}",
        f"Clean Drafts: false alarms {t['false_alarms']['count']} of {t['false_alarms']['total']}",
        f"Cost: ${Decimal(t['cost_usd']):.4f} over {ran} Films"
        + (f" ({len(summary['skipped'])} skipped)" if summary["skipped"] else ""),
        f"Watcher: {summary['watcher']}, ${Decimal(t['watcher']['cost_usd']):.4f}, "
        f"{t['watcher']['grounding_rejections']} grounding rejections, {t['watcher']['key_moments']} Key Moments",
    ]


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
    lines += [f"- {line}" for line in _totals_lines(summary)]
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
        lines += ["", "### Generated posts", ""]
        lines += [f"- **{c.status}** {c.post.platform} {c.post.language}: {c.post.text}" for c in r.posts] or [
            "- (none)"
        ]
        lines += ["", "### Published posts", ""]
        lines += [f"- {p.platform} {p.language}, {p.date} ({p.url}): {p.text}" for p in r.key.published_posts] or [
            "- (none in the Answer Key)"
        ]
    return "\n".join(lines) + "\n"
