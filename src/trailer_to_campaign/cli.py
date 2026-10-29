"""The t2c command line.

    t2c dummy-run --film sample --budget 0.001

dummy-run sends fake model calls through the Film Budget meter, so the hard
stop and the run log can be checked without an API key or any spend.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path

from trailer_to_campaign.budget import FilmBudgetReached, FilmMeter
from trailer_to_campaign.config import Settings, load_settings, parse_film_budget
from trailer_to_campaign.pricing import Usage
from trailer_to_campaign.run_log import RunLog, check_film_slug

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
    from trailer_to_campaign.tools import film

    create = film.create_spoiler_server if spoilers else film.create_catalog_server
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
    if args.command == "serve-footage":
        return serve_footage(settings)
    if args.command in ("serve-catalog", "serve-spoilers"):
        return serve_film(settings, spoilers=args.command == "serve-spoilers")
    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":
    sys.exit(main())
