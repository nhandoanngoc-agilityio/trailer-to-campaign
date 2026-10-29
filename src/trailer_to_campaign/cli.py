"""The t2c command line.

    t2c dummy-run --film sample --budget 0.001
    t2c watch --film agent-327 [--source trailer.mp4]
    t2c write --film agent-327 --key-moments runs/agent-327/<time>/key_moments.json

dummy-run sends fake model calls through the Film Budget meter, so the hard
stop and the run log can be checked without an API key or any spend.
watch runs the Watcher on a Film's trailer (the catalog's link unless --source
names one) and writes its Key Moments next to the Run Log. write runs the
Copywriter on those Key Moments and writes the Social Posts the same way; until
T7 joins them, each command is a Run of its own.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from trailer_to_campaign.budget import FilmBudgetReached, FilmMeter
from trailer_to_campaign.catalog import load_film
from trailer_to_campaign.config import MissingApiKey, Settings, load_settings, parse_film_budget
from trailer_to_campaign.pricing import Usage
from trailer_to_campaign.run_log import RunLog, check_film_slug

EXIT_AGENT_FAILED = 1
EXIT_FILM_BUDGET_REACHED = 3

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

    # No SDK retries: a retried call that timed out could be billed twice but metered once.
    return anthropic.AsyncAnthropic(api_key=api_key, max_retries=0)


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
    that can't be saved is printed instead, since it was paid for. Returns the exit
    code and the result (empty unless the Run completed).
    """
    import anyio

    from trailer_to_campaign.agents.loop import AgentFailed

    run_log = RunLog.create(runs_dir, film)
    meter = FilmMeter(budget, run_log)
    log.info("running the %s on %s with a Film Budget of $%s; log at %s", agent, film, budget, run_log.path)
    try:
        results = anyio.run(start, meter)  # start builds the servers and client, so a setup failure ends the Run
    except FilmBudgetReached as err:
        run_log.finish(meter.spent, "film_budget_reached")
        print(err, file=sys.stderr)
        return EXIT_FILM_BUDGET_REACHED, []
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
    run_log.finish(meter.spent, "completed")
    print(f"{len(results)} {noun} for {film}, ${meter.spent:.4f} of ${budget}: {out}")
    return 0, results


def watch(film: str, source: str | None, budget: Decimal, runs_dir: Path, settings: Settings) -> int:
    from trailer_to_campaign.agents import watcher

    try:
        api_key = settings.require_api_key()
        source = source or load_film(settings.films_dir, film).catalog.trailer
    except (MissingApiKey, ValueError) as err:  # ValueError: a bad or missing film.toml
        print(err, file=sys.stderr)
        return 2
    if not source:
        print(f"{film} has no trailer link in its film.toml; name one with --source", file=sys.stderr)
        return 2

    async def start(meter: FilmMeter) -> list[Any]:
        servers, client = watcher.watcher_servers(settings), make_client(api_key)
        return await watcher.watch_trailer(film, source, servers=servers, client=client, meter=meter)

    code, moments = run_agent(film, budget, runs_dir, "watcher", start, "key_moments.json", "Key Moments")
    for m in moments:
        line = f"  {m.start:7.1f}-{m.end:<7.1f} {m.mood}: {m.what_happens}"
        print(line + (f' "{m.quote.text}"' if m.quote else ""))
    return code


def write(film: str, key_moments_file: Path, budget: Decimal, runs_dir: Path, settings: Settings) -> int:
    from trailer_to_campaign.agents import copywriter
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
        servers, client = copywriter.copywriter_servers(settings), make_client(api_key)
        return await copywriter.write_posts(film, key_moments, servers=servers, client=client, meter=meter)

    code, posts = run_agent(film, budget, runs_dir, "copywriter", start, "social_posts.json", "Social Posts")
    for post in posts:
        print(f"  {post.platform:9} {post.language}: {post.text}")
    return code


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
    try:
        settings = load_settings(env)
    except ValueError as err:
        parser.error(str(err))
    if args.command == "dummy-run":
        return dummy_run(
            film=args.film,
            budget=args.budget or settings.film_budget_usd,
            calls=args.calls,
            runs_dir=args.runs_dir or settings.runs_dir,
        )
    if args.command == "watch":
        return watch(
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
    if args.command.startswith("serve-") and not settings.films_dir.is_dir():
        # An MCP host may start the server in / or $HOME, where there is no films/ folder.
        print(
            f"no films folder at {settings.films_dir.resolve()}; set T2C_FILMS_DIR to the project's films/ folder",
            file=sys.stderr,
        )
        return 2
    if args.command == "serve-footage":
        return serve_footage(settings)
    if args.command in ("serve-catalog", "serve-spoilers"):
        return serve_film(settings, spoilers=args.command == "serve-spoilers")
    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":
    sys.exit(main())
