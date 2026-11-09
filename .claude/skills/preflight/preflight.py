#!/usr/bin/env python3
"""Check this repo's environment before work starts (T30). Read-only: it reports and never repairs.

Every row is PASS, FAIL or UNKNOWN, and something it can't confirm is never a pass. Exit code: 0 when every check that
ran passed, 1 on any FAIL, 2 on UNKNOWN with no FAIL. It runs on the machine's python3 and macOS ships 3.9, so it uses
the standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

HOST = "github.com"
REPO = "nhandoanngoc-agilityio/trailer-to-campaign"

PASS, FAIL, UNKNOWN, SKIP = "PASS", "FAIL", "UNKNOWN", "SKIP"


class Row(NamedTuple):
    status: str
    name: str
    evidence: str
    fix: str = ""


class Ran(NamedTuple):
    outcome: str  # "ok" (it ran, whatever its exit code), "timeout" or "missing"
    code: int = -1
    out: str = ""
    err: str = ""


class Context(NamedTuple):
    root: Path
    timeout: float


def tell(text: str) -> str:
    """Make text safe to print: one line, no control characters, no credentials, not too long."""
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", text).strip()
    text = re.sub(r"://[^/@\s]+@", "://", text)
    text = re.sub(r"\bgh[a-z]_\w+|\bgithub_pat_\w+", "[token]", text)
    return text[:300]


def run(args: list, ctx_timeout: float, cwd: Path | None = None, stdin: str | None = None) -> Ran:
    """Run one command and never hang: it gets its own process group, and all of it dies at the timeout.

    subprocess.run(timeout=...) kills only the child, then waits for the pipe, which a grandchild can hold open.
    """
    env = dict(
        os.environ,
        GH_PROMPT_DISABLED="1",
        GH_NO_UPDATE_NOTIFIER="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_OPTIONAL_LOCKS="0",  # or `git status` may refresh the index, and this script changes nothing
        PYTHONDONTWRITEBYTECODE="1",
        NO_COLOR="1",
    )
    try:
        process = subprocess.Popen(
            args,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except (FileNotFoundError, NotADirectoryError):
        return Ran("missing")
    except PermissionError:
        return Ran("missing", err="not executable")
    try:
        out, err = process.communicate(stdin, timeout=ctx_timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:  # something that left the group could still hold the pipe, so this wait has a limit too
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        return Ran("timeout")
    return Ran("ok", process.returncode, out, err)


def parse_remote(url: str) -> tuple | None:
    """(host, owner/repo) from an https, ssh or scp-style remote, or None for anything else."""
    if "://" in url:
        parts = urlsplit(url)
        if parts.scheme not in ("https", "ssh") or not parts.hostname:
            return None
        host, path = parts.hostname, parts.path
    else:
        match = re.fullmatch(r"(?:[^@/:]+@)?([^@/:]+):([^:]+)", url)
        if not match:
            return None
        host, path = match.groups()
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return host.lower(), path


def is_this_repo(url: str) -> bool:
    parsed = parse_remote(url)
    return parsed is not None and parsed[0] == HOST and parsed[1].lower() == REPO.lower()


def shown_remote(url: str) -> str:
    """A remote for the report: host and path only, so credentials can't reach it."""
    parsed = parse_remote(url)
    return f"{parsed[0]}/{parsed[1]}" if parsed else "a remote that isn't an https, ssh or scp-style URL"


def check_remote(ctx: Context) -> Row:
    fix = f"git remote set-url origin https://{HOST}/{REPO}.git"
    urls = []
    for kind, flags in (("fetch", []), ("push", ["--push"])):
        ran = run(["git", "remote", "get-url", *flags, "origin"], ctx.timeout, ctx.root)
        if ran.outcome == "timeout":
            return Row(UNKNOWN, "remote", "git timed out reading origin", "Run: git remote -v")
        if ran.outcome != "ok" or ran.code != 0:
            return Row(FAIL, "remote", "this repo has no origin remote", fix)
        urls.append((kind, ran.out.strip()))
    wrong = [(kind, url) for kind, url in urls if not is_this_repo(url)]
    if wrong:
        shown = ", ".join(f"{kind} is {shown_remote(url)}" for kind, url in wrong)
        return Row(FAIL, "remote", f"origin {shown}; expected {HOST}/{REPO}", fix)
    return Row(PASS, "remote", f"origin fetch and push are {HOST}/{REPO}")


def check_github_access(ctx: Context) -> Row:
    # No repo argument: this asks what `gh` would really act on here, which a stray GH_REPO would change.
    ran = run(["gh", "repo", "view", "--json", "nameWithOwner,url"], ctx.timeout, ctx.root)
    name = "github access"
    if ran.outcome == "missing":
        return Row(FAIL, name, "gh is not installed", "brew install gh")
    if ran.outcome == "timeout":
        return Row(UNKNOWN, name, f"gh gave no answer in {ctx.timeout:g}s", "Check the network, then run: gh repo view")
    if ran.code != 0:
        return Row(FAIL, name, f"gh repo view failed: {ran.err or ran.out}", "Run: gh auth status, then gh auth login")
    try:
        data = json.loads(ran.out)
        found, url = data["nameWithOwner"], data["url"]
        host = urlsplit(url).hostname
    except (ValueError, KeyError, TypeError, AttributeError):
        return Row(
            UNKNOWN, name, f"gh's answer couldn't be read: {ran.out}", "Run: gh repo view --json nameWithOwner,url"
        )
    if str(found).lower() != REPO.lower() or host != HOST:
        fix = "Check GH_REPO and GH_HOST, and the remotes in: git remote -v"
        return Row(FAIL, name, f"gh would act on {url}, not {HOST}/{REPO}", fix)
    return Row(PASS, name, f"gh reads {HOST}/{REPO}")


CHECKS: list = [("remote", check_remote), ("github access", check_github_access)]


def guarded(name: str, check: Callable[[Context], Row], ctx: Context) -> Row:
    """A check that crashes reports UNKNOWN, and the others still run."""
    try:
        return check(ctx)
    except Exception as error:
        return Row(UNKNOWN, name, f"the check itself failed: {error!r}", "Read the check in preflight.py")


def print_row(row: Row) -> None:
    print(f"{row.status:<8}{row.name:<15}{tell(row.evidence)}")
    if row.fix:
        print(f"{'':<8}{'':<15}fix: {tell(row.fix)}")


def exit_code(rows: list) -> int:
    statuses = {row.status for row in rows}
    if FAIL in statuses:
        return 1
    return 2 if UNKNOWN in statuses else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Check this repo's environment. Read-only.")
    parser.add_argument("--timeout", type=float, default=20.0, help="seconds to allow each external call")
    args = parser.parse_args()

    ran = run(["git", "rev-parse", "--show-toplevel"], args.timeout)
    if ran.outcome != "ok" or ran.code != 0:
        print_row(Row(FAIL, "remote", "not inside a git repo", "Run /preflight from the repo"))
        return 1
    ctx = Context(Path(ran.out.strip()), args.timeout)

    rows = []
    for name, check in CHECKS:
        rows.append(guarded(name, check, ctx))
        print_row(rows[-1])
    return exit_code(rows)


if __name__ == "__main__":
    sys.exit(main())
