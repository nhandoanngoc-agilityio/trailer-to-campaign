"""The t2c command line.

    t2c dummy-run --film sample --budget 0.001
    t2c run --film agent-327 [--source trailer.mp4]
    t2c watch --film agent-327 [--source trailer.mp4]
    t2c write --film agent-327 --key-moments runs/agent-327/<time>/key_moments.json
    t2c eval [--eval-dir evals]

run is one Run for a Film: the Watcher, the Approval of its Key Moments, the
Copywriter and the Checker, then the Approval of the Social Posts, in this
terminal, under one Film Budget and one Run Log.
dummy-run sends fake model calls through the Film Budget meter, so the hard
stop and the run log can be checked without an API key or any spend.
watch runs the Watcher on a Film's trailer (the catalog's link unless --source
names one) and writes its Key Moments next to the Run Log. write runs the
Copywriter and the Checker on those Key Moments and writes the Social Posts the
same way: no post leaves it unchecked. watch and write are single stages, each a
Run of its own, for trying one agent; run is the Run a marketer uses.
eval runs the Eval Set (ADR 0008): one Eval Set Run per Film, scored against its
Answer Key, with no Approval, so it needs no person and runs unattended.
run, watch, eval and slack take --watcher v0|sdk: the Watcher on its own loop (the
default) or on the Claude Agent SDK (T11).
result writes a real Film's side-by-side page (T12): Time Saved from the two
marketers' stopwatch times, and the Approval Rate and cost per Film from its Run Logs.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from trailer_to_campaign.budget import FilmBudgetReached, FilmMeter
from trailer_to_campaign.catalog import default_source, load_film
from trailer_to_campaign.config import (
    WATCHERS,
    MissingApiKey,
    MissingSlackSettings,
    Settings,
    load_settings,
    parse_film_budget,
)
from trailer_to_campaign.pricing import Usage
from trailer_to_campaign.run_log import EXIT_AGENT_FAILED, EXIT_FILM_BUDGET_REACHED, RunLog, check_film_slug

DUMMY_MODEL = "claude-opus-5-5"
DUMMY_INPUT_TOKENS = 2_000
DUMMY_MAX_TOKENS = 1_000
DUMMY_USAGE = Usage(input=DUMMY_INPUT_TOKENS, output=500)

log = logging.getLogger("trailer_to_campaign")


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _budget_arg(value: str) -> Decimal:
    try:
        return parse_film_budget(value, source="--budget")
    except ValueError as err:
        raise argparse.ArgumentTypeError(str(err)) from err


def _parallel_arg(value: str) -> int:
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"--parallel must be a whole number of Films, at least 1 (got {value!r})")
    return int(value)


def _film_arg(value: str) -> str:
    try:
        return check_film_slug(value)
    except ValueError as err:
        raise argparse.ArgumentTypeError(str(err)) from err


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="t2c", description="Turn a Film's trailer into Social Posts.")
    parser.add_argument("-v", "--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    dummy = commands.add_parser("dummy-run", help="send fake model calls through the Film Budget meter")
    dummy.add_argument("--film", type=_film_arg, default="sample")
    dummy.add_argument("--budget", type=_budget_arg, help="Film Budget in USD (default: FILM_BUDGET_USD or 2.00)")
    dummy.add_argument("--calls", type=int, default=5, help="how many fake model calls to make")
    dummy.add_argument("--runs-dir", type=Path, help="where run logs go (default: runs/)")

    run = commands.add_parser("run", help="one Run for a Film: watch, approve, write and check, approve")
    run.add_argument("--film", type=_film_arg, required=True)
    run.add_argument("--source", help="a file in films/<film>/source/ or a trailer link (default: the catalog's)")
    run.add_argument("--budget", type=_budget_arg, help="Film Budget in USD (default: FILM_BUDGET_USD or 2.00)")
    run.add_argument("--runs-dir", type=Path, help="where run logs go (default: runs/)")

    watch = commands.add_parser("watch", help="run the Watcher on a Film's trailer and write its Key Moments")
    watch.add_argument("--film", type=_film_arg, required=True)
    watch.add_argument("--source", help="a file in films/<film>/source/ or a trailer link (default: the catalog's)")
    watch.add_argument("--budget", type=_budget_arg, help="Film Budget in USD (default: FILM_BUDGET_USD or 2.00)")
    watch.add_argument("--runs-dir", type=Path, help="where run logs go (default: runs/)")

    write = commands.add_parser("write", help="run the Copywriter on Key Moments and write its Social Posts")
    write.add_argument("--film", type=_film_arg, required=True)
    write.add_argument("--key-moments", type=Path, required=True, help="a key_moments.json written by t2c watch")
    write.add_argument("--budget", type=_budget_arg, help="Film Budget in USD (default: FILM_BUDGET_USD or 2.00)")
    write.add_argument("--runs-dir", type=Path, help="where run logs go (default: runs/)")

    evaluate = commands.add_parser("eval", help="run the Eval Set and report how many Traps the Checker caught")
    evaluate.add_argument("--eval-dir", type=Path, default=Path("evals"), help="the Eval Set (default: evals/)")
    evaluate.add_argument("--budget", type=_budget_arg, help="Film Budget per Run (default: FILM_BUDGET_USD or 2.00)")
    evaluate.add_argument("--runs-dir", type=Path, help="where run logs and the report go (default: runs/)")
    evaluate.add_argument(
        "--parallel", type=_parallel_arg, help="how many Films run at once (default: all; 1 runs them in turn)"
    )
    scope = evaluate.add_mutually_exclusive_group()
    scope.add_argument(
        "--only", choices=["drafts"], help="drafts: only the Checker on the Planted and Clean Drafts (about $0.15)"
    )
    scope.add_argument(
        "--key-moments-from",
        type=Path,
        metavar="REPORT_DIR",
        help="reuse the Key Moments of an earlier run's report (runs/_eval/<time>): no Watcher runs",
    )
    scope.add_argument(
        "--baseline",
        action="store_true",
        help="print the newest saved report of main's agents, or say a new run is needed; free",
    )

    slack = commands.add_parser("slack", help="run the Slack front door: a Marketer's @mention starts a Run")
    slack.add_argument("--budget", type=_budget_arg, help="Film Budget per Run in USD (default: FILM_BUDGET_USD)")
    slack.add_argument("--runs-dir", type=Path, help="where run logs go (default: runs/)")

    result = commands.add_parser("result", help="the side-by-side result for a real Film: Time Saved, Approval, cost")
    result.add_argument("--film", type=_film_arg, required=True)
    result.add_argument("--old-way", required=True, help="minutes the old way took, from that marketer's stopwatch")
    result.add_argument("--new-way", required=True, help="minutes Trailer-to-Campaign took, timed the same way")
    result.add_argument("--runs-dir", type=Path, help="where the Film's Run Logs are (default: runs/)")
    result.add_argument(
        "--run", help="the Run whose Campaign was used, by its folder (default: the last to finish that gave one)"
    )

    for watching in (run, watch, evaluate, slack):
        watching.add_argument(
            "--watcher", choices=WATCHERS, help="v0 (its own loop) or sdk (the Agent SDK); default: T2C_WATCHER or v0"
        )

    commands.add_parser("serve-footage", help="run the footage MCP server on stdio (ADR 0002)")
    commands.add_parser("serve-catalog", help="run the Catalog Metadata MCP server on stdio")
    commands.add_parser("serve-spoilers", help="run the Spoiler List MCP server on stdio (the Checker's only)")
    return parser


def dummy_run(film: str, budget: Decimal, calls: int, runs_dir: Path) -> int:
    run_log = RunLog.create(runs_dir, film)
    meter = FilmMeter(budget, run_log)
    log.info("dummy run for %s with a Film Budget of $%s; log at %s", film, budget, run_log.path)
    try:
        for _ in range(calls):
            with meter.call(DUMMY_MODEL, DUMMY_INPUT_TOKENS, DUMMY_MAX_TOKENS, agent="dummy") as call:
                call.record(DUMMY_USAGE)
    except FilmBudgetReached as err:
        run_log.finish(meter.spent, "film_budget_reached")
        print(err, file=sys.stderr)
        return EXIT_FILM_BUDGET_REACHED
    except BaseException:
        run_log.finish(meter.spent, "error")  # Ctrl-C or a crash still leaves the total
        raise
    run_log.finish(meter.spent, "completed")
    log.info("dummy run completed: spent $%s of $%s", meter.spent, budget)
    return 0


def make_client(api_key: str) -> Any:
    import anthropic

    from trailer_to_campaign.agents.loop import MODEL_CALL_TIMEOUT_S

    # No SDK retries: a retried call that timed out could be billed twice but metered once.
    # Timeout (T17): MODEL_CALL_TIMEOUT_S, 600 s, the most the SDK allows a non-streaming call
    # (why: next to the constant). This one is per read, so it alone let a call hang 25+ minutes
    # (#34); the agent loop holds each call to it end to end. Connecting still fails in 5 s.
    timeout = anthropic.Timeout(MODEL_CALL_TIMEOUT_S, connect=5.0)
    return anthropic.AsyncAnthropic(api_key=api_key, max_retries=0, timeout=timeout)


def run_agent(
    film: str,
    budget: Decimal,
    runs_dir: Path,
    agent: str,
    start: Callable[[FilmMeter], Awaitable[list[Any]]],
    out_name: str,
    noun: str,
) -> tuple[int, list[Any]]:
    """One agent in one Run: its Run Log, the Film Budget, and its result saved next to the log.

    The Run always ends in the log: completed, film_budget_reached, or error. A result
    that can't be saved is printed instead, since it was paid for. If the Film Budget
    runs out after some of the work is finished (FilmBudgetReachedMidReview carries it
    as .checked), that part is saved too. Returns the exit code and the saved result.
    """
    import anyio

    from trailer_to_campaign.agents.loop import AgentFailed

    run_log = RunLog.create(runs_dir, film)
    meter = FilmMeter(budget, run_log)
    log.info("running the %s on %s with a Film Budget of $%s; log at %s", agent, film, budget, run_log.path)
    stop, code = "completed", 0
    try:
        results = anyio.run(start, meter)  # start builds the servers and client, so a setup failure ends the Run
    except FilmBudgetReached as err:
        print(err, file=sys.stderr)
        stop, code = "film_budget_reached", EXIT_FILM_BUDGET_REACHED
        results = list(getattr(err, "checked", []))  # paid for and finished before the budget ran out
        if not results:
            run_log.finish(meter.spent, stop)
            return code, []
    except AgentFailed as err:
        run_log.finish(meter.spent, "error")
        print(err, file=sys.stderr)
        return EXIT_AGENT_FAILED, []
    except BaseException:
        run_log.finish(meter.spent, "error")
        raise

    out = run_log.path.parent / out_name
    saved = json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2)
    try:
        out.write_text(saved, encoding="utf-8")
    except OSError as err:
        run_log.finish(meter.spent, "error")
        print(saved)
        print(f"could not save the {noun} to {out} ({type(err).__name__}); printed above", file=sys.stderr)
        return EXIT_AGENT_FAILED, []
    meter.event(out.stem, agent=agent, count=len(results), file=out)
    run_log.finish(meter.spent, stop)
    print(f"{len(results)} {noun} for {film}, ${meter.spent:.4f} of ${budget}: {out}")
    return code, results


def _key_and_trailer(film: str, source: str | None, settings: Settings) -> tuple[str, str] | None:
    """The API key and the trailer to watch, checked before any Run starts; None after saying what's wrong."""
    try:
        api_key = settings.require_api_key()
        source = source or default_source(load_film(settings.films_dir, film), settings.films_dir)
    except (MissingApiKey, ValueError) as err:  # ValueError: a bad or missing film.toml
        print(err, file=sys.stderr)
        return None
    if not source:
        print(f"{film} has no trailer link in its film.toml; name one with --source", file=sys.stderr)
        return None
    return api_key, source


def _at_a_terminal() -> bool:
    """Approval must be typed by a person: `yes a | t2c run` would approve every draft unseen."""
    return sys.stdin.isatty()


def run(film: str, source: str | None, budget: Decimal, runs_dir: Path, settings: Settings) -> int:
    from trailer_to_campaign import pipeline
    from trailer_to_campaign.approval import TerminalApprover

    if not _at_a_terminal():
        print("t2c run needs a person at a terminal: only typed Approval counts, never piped input", file=sys.stderr)
        return 2
    checked = _key_and_trailer(film, source, settings)
    if checked is None:
        return 2
    api_key, source = checked
    stages = pipeline.real_stages(film, source, settings, lambda: make_client(api_key))
    return pipeline.run_film(film, budget, runs_dir, stages, TerminalApprover())


def watch(film: str, source: str | None, budget: Decimal, runs_dir: Path, settings: Settings) -> int:
    from trailer_to_campaign.pipeline import watch_stage

    checked = _key_and_trailer(film, source, settings)
    if checked is None:
        return 2
    api_key, source = checked
    start = watch_stage(film, source, settings, lambda: make_client(api_key))
    code, moments = run_agent(film, budget, runs_dir, "watcher", start, "key_moments.json", "Key Moments")
    for m in moments:
        line = f"  {m.start:7.1f}-{m.end:<7.1f} {m.mood}: {m.what_happens}"
        print(line + (f' "{m.quote.text}"' if m.quote else ""))
    return code


def write(film: str, key_moments_file: Path, budget: Decimal, runs_dir: Path, settings: Settings) -> int:
    from trailer_to_campaign.agents import review
    from trailer_to_campaign.agents.key_moments import load_key_moments

    try:
        api_key = settings.require_api_key()
        load_film(settings.films_dir, film)  # a typo'd Film would only fail after paid calls
        key_moments = load_key_moments(key_moments_file)
    except (MissingApiKey, ValueError) as err:  # ValueError: a bad Film or Key Moments file
        print(err, file=sys.stderr)
        return 2
    watched_for = key_moments_file.resolve().parent.parent.name  # t2c watch writes <runs>/<film>/<time>/
    if watched_for != film:
        print(
            f"{key_moments_file.name} was not watched for {film} (it is under {watched_for!r}); "
            f"use the one t2c watch wrote under runs/{film}/",
            file=sys.stderr,
        )
        return 2

    async def start(meter: FilmMeter) -> list[Any]:
        # The whole loop, never the Copywriter alone: no post leaves this command unchecked.
        from trailer_to_campaign.pipeline import stage_client

        async with stage_client(lambda: make_client(api_key)) as client:
            agents = review.agents_for(film, settings, client, meter)
            return await review.write_and_check(key_moments, agents, meter)

    code, checked = run_agent(film, budget, runs_dir, "copywriter+checker", start, "social_posts.json", "Social Posts")
    for c in checked:
        print(f"  {c.status:7} {c.post.platform:9} {c.post.language}: {c.post.text}")
        for problem in c.problems:
            print(f"          ! {problem}")
    if checked:
        flagged = sum(c.status == "flagged" for c in checked)
        print(f"{len(checked) - flagged} passed, {flagged} flagged for a person")
    return code


def run_eval_set(
    eval_dir: Path,
    budget: Decimal,
    runs_dir: Path,
    settings: Settings,
    parallel: int | None = None,
    only_drafts: bool = False,
    key_moments_from: Path | None = None,
    baseline: bool = False,
) -> int:
    from trailer_to_campaign import evals

    if baseline:
        try:
            present = [film.slug for film in evals.prepare(eval_dir) if film.key is not None]
        except ValueError as err:
            print(err, file=sys.stderr)
            return 2
        return evals.show_baseline(runs_dir, settings.watcher, present=present)
    try:
        api_key = settings.require_api_key()
        films = evals.prepare(eval_dir)  # every Answer Key is checked before the first paid call
        reused = evals.load_reused(key_moments_from, films) if key_moments_from else None
    except (MissingApiKey, ValueError) as err:  # ValueError: a bad eval_set.toml, film.toml, Answer Key or report
        print(err, file=sys.stderr)
        return 2
    settings = replace(settings, films_dir=eval_dir / "films")
    return evals.run_eval(
        films,
        budget,
        runs_dir,
        lambda film: evals.real_stages(film.slug, film.trailer, settings, lambda: make_client(api_key)),
        watcher=settings.watcher,
        parallel=parallel,
        only_drafts=only_drafts,
        reused=reused,
    )


def slack(budget: Decimal, runs_dir: Path, settings: Settings) -> int:
    """The Slack front door (ADR 0002) over Socket Mode, until Ctrl-C.

    Each Marketer's @mention is one Run: Approval comes from that Run Thread's buttons
    and its progress is posted there. Ctrl-C ends a waiting Run in an error, never an approval.
    """
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    from trailer_to_campaign import pipeline, slack_app

    try:
        bot_token, app_token = settings.require_slack()
        api_key = settings.require_api_key()
    except (MissingSlackSettings, MissingApiKey) as err:
        print(err, file=sys.stderr)
        return 2

    def runner(request: Any, approver: Any, thread: Any) -> int:
        stages = pipeline.real_stages(request.film, request.source, settings, lambda: make_client(api_key))
        return pipeline.run_film(request.film, budget, runs_dir, stages, approver, out=thread)

    door = slack_app.FrontDoor(settings.films_dir, settings.marketers, runner)
    app = slack_app.build_app(bot_token, door)
    log.info(
        "Slack front door is up for %d Marketer(s); runs go to %s. Ctrl-C to stop.", len(settings.marketers), runs_dir
    )
    try:
        SocketModeHandler(app, app_token).start()
    except KeyboardInterrupt:
        # A Run waiting on Approval ends now. One mid-stage finishes that stage (inside its Film Budget)
        # and ends at its next Approval point: stopping it mid-call would lose its Run Log's total.
        log.info("stopping: a Run waiting on Approval ends now, in an error; one mid-stage ends at its next Approval")
    finally:
        door.close()
    return 0


def side_by_side(
    film: str, old_way: str, new_way: str, runs_dir: Path, settings: Settings, used_run: str | None = None
) -> int:
    """T12's one-page result from the Film's Run Logs and the two stopwatch times. Reads files only."""
    from trailer_to_campaign.result import film_result, write_result

    try:
        result = film_result(
            film, runs_dir, settings.films_dir, old_minutes=old_way, new_minutes=new_way, used_run=used_run
        )
    except ValueError as err:
        print(err, file=sys.stderr)
        return 2
    try:
        page = write_result(result, runs_dir)
    except OSError as err:  # never pass the OS message on: it holds the local path
        print(f"could not write the result for {film} ({type(err).__name__})", file=sys.stderr)
        return 2
    print(page.read_text(encoding="utf-8"))
    print(f"Written: {_where(runs_dir, film)} (gitignored: share the numbers once the team agrees)")
    return 0


def _where(runs_dir: Path, film: str) -> str:
    """Where the result went, without printing a local path the person didn't type."""
    if not runs_dir.is_absolute():
        return f"{runs_dir / film / 'result.md'} and result.json"
    try:
        return f"{runs_dir.relative_to(Path.cwd()) / film / 'result.md'} and result.json"
    except ValueError:
        return f"{film}/result.md and result.json under the --runs-dir folder"


def serve_footage(settings: Settings) -> int:
    from trailer_to_campaign.footage.sources import YtDlpDownloader
    from trailer_to_campaign.footage.transcript import FasterWhisperTranscriber
    from trailer_to_campaign.tools.footage import create_server

    server = create_server(
        # Absolute, because an MCP host may start this server in / or $HOME.
        films_dir=settings.films_dir.resolve(),
        downloader=YtDlpDownloader(),
        transcriber=FasterWhisperTranscriber(settings.whisper_model),
    )
    server.run()  # stdio: stdout carries MCP messages, so logs go to stderr
    return 0


def serve_film(settings: Settings, spoilers: bool) -> int:
    from trailer_to_campaign.tools import catalog
    from trailer_to_campaign.tools import spoilers as spoiler_tool

    create = spoiler_tool.create_spoiler_server if spoilers else catalog.create_catalog_server
    create(settings.films_dir.resolve()).run()  # absolute: an MCP host may start this in / or $HOME
    return 0


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    env = os.environ if env is None else env
    if getattr(args, "budget", None) is not None:
        env = {k: v for k, v in env.items() if k != "FILM_BUDGET_USD"}  # --budget wins
    if getattr(args, "watcher", None) is not None or not hasattr(args, "watcher"):
        env = {k: v for k, v in env.items() if k != "T2C_WATCHER"}  # --watcher wins; unused without a Watcher
    try:
        settings = load_settings(env)
    except ValueError as err:
        parser.error(str(err))
    if getattr(args, "watcher", None):
        settings = replace(settings, watcher=args.watcher)
    if args.command == "dummy-run":
        return dummy_run(
            film=args.film,
            budget=args.budget or settings.film_budget_usd,
            calls=args.calls,
            runs_dir=args.runs_dir or settings.runs_dir,
        )
    if args.command in ("run", "watch"):
        command = run if args.command == "run" else watch
        return command(
            film=args.film,
            source=args.source,
            budget=args.budget or settings.film_budget_usd,
            runs_dir=args.runs_dir or settings.runs_dir,
            settings=settings,
        )
    if args.command == "write":
        return write(
            film=args.film,
            key_moments_file=args.key_moments,
            budget=args.budget or settings.film_budget_usd,
            runs_dir=args.runs_dir or settings.runs_dir,
            settings=settings,
        )
    if args.command == "result":
        return side_by_side(
            film=args.film,
            old_way=args.old_way,
            new_way=args.new_way,
            runs_dir=args.runs_dir or settings.runs_dir,
            settings=settings,
            used_run=args.run,
        )
    if args.command == "eval":
        return run_eval_set(
            eval_dir=args.eval_dir,
            budget=args.budget or settings.film_budget_usd,
            runs_dir=args.runs_dir or settings.runs_dir,
            settings=settings,
            parallel=args.parallel,
            only_drafts=args.only == "drafts",
            key_moments_from=args.key_moments_from,
            baseline=args.baseline,
        )
    if (args.command.startswith("serve-") or args.command == "slack") and not settings.films_dir.is_dir():
        # An MCP host may start the server in / or $HOME, where there is no films/ folder.
        print(
            f"no films folder at {settings.films_dir.resolve()}; set T2C_FILMS_DIR to the project's films/ folder",
            file=sys.stderr,
        )
        return 2
    if args.command == "slack":
        return slack(
            budget=args.budget or settings.film_budget_usd,
            runs_dir=args.runs_dir or settings.runs_dir,
            settings=settings,
        )
    if args.command == "serve-footage":
        return serve_footage(settings)
    if args.command in ("serve-catalog", "serve-spoilers"):
        return serve_film(settings, spoilers=args.command == "serve-spoilers")
    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":
    sys.exit(main())
