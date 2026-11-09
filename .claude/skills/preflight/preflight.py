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


def run(
    args: list, ctx_timeout: float, cwd: Path | None = None, stdin: str | None = None, env_extra: dict | None = None
) -> Ran:
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
        **(env_extra or {}),
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
    except OSError as error:  # not there, not executable, or not a program at all: it can't run, whichever
        return Ran("missing", err=type(error).__name__)
    try:
        out, err = process.communicate(stdin, timeout=ctx_timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):  # macOS can answer PermissionError for a group that's ending
            process.kill()
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
    good = f"https://{HOST}/{REPO}.git"
    urls = []
    # --all, because `git push` sends to every push URL and without it git shows only the first.
    for kind, flags in (("fetch", []), ("push", ["--push"])):
        ran = run(["git", "remote", "get-url", "--all", *flags, "origin"], ctx.timeout, ctx.root)
        if ran.outcome == "timeout":
            return Row(UNKNOWN, "remote", "git timed out reading origin", "Run: git remote -v")
        if ran.outcome != "ok" or ran.code != 0:
            return Row(FAIL, "remote", "this repo has no origin remote", f"git remote add origin {good}")
        found = ran.out.split()
        if not found:  # nothing seen isn't the same as nothing wrong
            fix = "git config --unset-all remote.origin.pushurl" if flags else f"git remote set-url origin {good}"
            return Row(FAIL, "remote", f"origin has no {kind} URL", fix)
        urls.extend((kind, url) for url in found)
    wrong = [(kind, url) for kind, url in urls if not is_this_repo(url)]
    if wrong:
        fixes = []
        if any(kind == "fetch" for kind, _ in wrong):
            fixes.append(f"git remote set-url origin {good}")
        if any(kind == "push" for kind, _ in wrong):
            several = sum(kind == "push" for kind, _ in urls) > 1
            fixes.append(
                "git config --unset-all remote.origin.pushurl"
                if several
                else f"git remote set-url --push origin {good}"
            )
        shown = ", ".join(f"{kind} is {shown_remote(url)}" for kind, url in wrong)
        return Row(FAIL, "remote", f"origin {shown}; expected {HOST}/{REPO}", "; ".join(fixes))
    return Row(PASS, "remote", f"origin fetch and push are {HOST}/{REPO}")


def gh_repo_is_this_repo(value: str) -> bool:
    """GH_REPO is [HOST/]OWNER/REPO, and gh also takes a URL."""
    if ":" in value:
        return is_this_repo(value)
    parts = value.split("/")
    return is_this_repo(f"https://{HOST}/{value}") if len(parts) == 2 else is_this_repo(f"https://{value}")


def check_github_access(ctx: Context) -> Row:
    name = "github access"
    # Read directly, because `gh repo view` ignores GH_REPO and reports this repo while `gh pr` and `gh issue` obey it.
    gh_repo = os.environ.get("GH_REPO", "").strip()
    if gh_repo and not gh_repo_is_this_repo(gh_repo):
        return Row(FAIL, name, f"GH_REPO sends gh pr and gh issue to {gh_repo}, not {HOST}/{REPO}", "unset GH_REPO")
    # No repo argument: naming it would pass whatever this folder's remotes say.
    ran = run(["gh", "repo", "view", "--json", "nameWithOwner,url"], ctx.timeout, ctx.root)
    if ran.outcome == "missing":
        return Row(FAIL, name, "gh is not installed", "brew install gh")
    if ran.outcome == "timeout":
        return Row(UNKNOWN, name, f"gh gave no answer in {ctx.timeout:g}s", "Check the network, then run: gh repo view")
    if ran.code != 0:
        text = ran.err or ran.out
        if re.search(r"error connecting|dial tcp|no such host|connection (refused|reset)|timed? ?out|HTTP 5\d\d", text):
            return Row(UNKNOWN, name, f"gh couldn't reach GitHub: {text}", "Check the network, then run: gh repo view")
        return Row(FAIL, name, f"gh repo view failed: {text}", "Run: gh auth status, then gh auth login")
    try:
        data = json.loads(ran.out)
        found, url = data["nameWithOwner"], data["url"]
        host = urlsplit(url).hostname
    except (ValueError, KeyError, TypeError, AttributeError):
        return Row(
            UNKNOWN, name, f"gh's answer couldn't be read: {ran.out}", "Run: gh repo view --json nameWithOwner,url"
        )
    if str(found).lower() != REPO.lower() or host != HOST:
        fix = "Check GH_HOST, and the remotes in: git remote -v"
        return Row(FAIL, name, f"gh would act on {url}, not {HOST}/{REPO}", fix)
    return Row(PASS, name, f"gh reads {HOST}/{REPO}")


def check_git_hook(ctx: Context) -> Row:
    name = "git hook"
    fix = "git config core.hooksPath .githooks"
    ran = run(["git", "config", "core.hooksPath"], ctx.timeout, ctx.root)
    if ran.outcome == "timeout":
        return Row(UNKNOWN, name, "git timed out reading core.hooksPath", f"Check it, or run: {fix}")
    value = ran.out.strip()
    if value != ".githooks":
        return Row(FAIL, name, f"core.hooksPath is {value!r}, not '.githooks', so the commit hook won't run", fix)
    if not os.access(ctx.root / ".githooks" / "pre-commit", os.X_OK):
        return Row(FAIL, name, ".githooks/pre-commit is missing or can't run", "chmod +x .githooks/pre-commit")
    return Row(PASS, name, "core.hooksPath is .githooks and pre-commit can run")


def matcher_covers_bash(entry: dict) -> bool:
    matcher = entry.get("matcher", "")
    return matcher in ("", "*") or "Bash" in str(matcher).split("|")


def hook_commands(settings: dict) -> list:
    """Every (event, entry, hook) in a settings file's hooks. Only a command hook runs a command."""
    found = []
    for event, entries in settings.get("hooks", {}).items():
        for entry in entries:
            for hook in entry.get("hooks", []):
                if hook.get("type") == "command":
                    found.append((event, entry, hook))
    return found


def send_to_hook(wired: str, command: str, limit: float, ctx: Context) -> Ran:
    """Give the wired hook a Bash call the way Claude Code does: the settings' own command, with the call on stdin.

    That command is what really stands between Claude and the shell, so a `|| true` on it counts. `command` is only
    text in JSON: nothing executes it.
    """
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(ctx.root)}
    env = {"CLAUDE_PROJECT_DIR": str(ctx.root)}
    return run(["/bin/sh", "-c", wired], limit, ctx.root, stdin=json.dumps(payload), env_extra=env)


def user_settings_problem() -> str | None:
    """Read one setting from the user's own settings, which also apply here. Nothing else in the file is used.

    Returns "off" if they turn hooks off, "unreadable" if that can't be told, and None otherwise.
    """
    path = Path.home() / ".claude" / "settings.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unreadable"
    if not isinstance(data, dict):
        return "unreadable"
    return "off" if data.get("disableAllHooks") else None


def check_claude_hooks(ctx: Context) -> Row:
    name = "claude hooks"
    fix = "Restore .claude/settings.json and .claude/hooks/ from main, and remove any disableAllHooks"
    settings: dict = {}
    for file in ("settings.json", "settings.local.json"):
        path = ctx.root / ".claude" / file
        if not path.exists() and file == "settings.local.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return Row(FAIL, name, f".claude/{file} can't be read: {type(error).__name__}", fix)
        if not isinstance(data, dict):
            return Row(FAIL, name, f".claude/{file} isn't a settings object", fix)
        if data.get("disableAllHooks"):
            return Row(FAIL, name, f".claude/{file} sets disableAllHooks, so no guardrail runs", fix)
        if file == "settings.json":
            settings = data
    user = user_settings_problem()
    if user == "off":
        fix = "Remove disableAllHooks from ~/.claude/settings.json"
        return Row(FAIL, name, "~/.claude/settings.json sets disableAllHooks, so no guardrail runs", fix)

    commands = hook_commands(settings)
    for _, _, hook in commands:
        for script in re.findall(r"\$CLAUDE_PROJECT_DIR/([^\"'\s]+)", str(hook.get("command", ""))):
            if not (ctx.root / script).is_file():
                return Row(FAIL, name, f"settings.json runs {script}, which doesn't exist", fix)
    guard = ".claude/hooks/block_forbidden.py"
    wired = [
        hook
        for event, entry, hook in commands
        if event == "PreToolUse" and matcher_covers_bash(entry) and guard in str(hook.get("command", ""))
    ]
    if not wired:
        return Row(FAIL, name, "block_forbidden.py isn't wired to Bash in PreToolUse", fix)
    if len(wired) > 1:
        return Row(UNKNOWN, name, f"block_forbidden.py is wired {len(wired)} times, and only one could be tried", fix)
    hook = wired[0]
    if hook.get("async"):
        return Row(FAIL, name, "block_forbidden.py is wired as async, so it can't block anything", fix)
    if "if" in hook:  # running the command directly can't show whether this would stop it running at all
        return Row(UNKNOWN, name, f"the wired hook has a condition, so it may not always run: {hook['if']}", fix)

    # The settings' own limit is the one that applies in Claude Code, so a hook has to answer inside it.
    declared = hook.get("timeout")
    has_limit = isinstance(declared, (int, float)) and not isinstance(declared, bool) and declared > 0
    limit = min(ctx.timeout, declared) if has_limit else ctx.timeout
    wired_command = str(hook.get("command", ""))
    blocked = send_to_hook(wired_command, "git reset --hard HEAD~1", limit, ctx)
    allowed = send_to_hook(wired_command, "git status", limit, ctx)
    if "timeout" in (blocked.outcome, allowed.outcome):
        if has_limit and declared <= ctx.timeout:
            return Row(FAIL, name, f"the wired hook went past its own {declared:g}s limit in settings.json", fix)
        return Row(UNKNOWN, name, f"the wired hook gave no answer in {limit:g}s", "Run tests/test_hooks.py")
    # Exit 2 alone isn't a block: the hook also exits 2 when it crashes. Only the wording tells them apart.
    if blocked.code != 2 or "Blocked by team policy" not in blocked.err:
        return Row(FAIL, name, f"the wired hook didn't block a forbidden command (exit {blocked.code})", fix)
    if allowed.code != 0 or allowed.out.strip():
        return Row(FAIL, name, f"the wired hook stopped or questioned a harmless command (exit {allowed.code})", fix)
    if user == "unreadable":
        fix = "Fix the JSON in ~/.claude/settings.json"
        return Row(UNKNOWN, name, "~/.claude/settings.json can't be read, and it could turn hooks off", fix)
    return Row(PASS, name, "the wired block_forbidden.py blocks a forbidden command and allows a harmless one")


def ci_python(text: str) -> str | None:
    """The one Python version in ci.yml, or None unless there's exactly one, written plainly.

    An exact line match, because the machine's python3 has no YAML parser. tests/test_preflight.py compares this with
    PyYAML's reading of the real file, so the two can't drift apart.
    """
    lines = re.findall(r"^[ \t]*python-version:.*$", text, re.MULTILINE)
    if len(lines) != 1:
        return None
    match = re.fullmatch(r"[ \t]*python-version:[ \t]*\"(\d+\.\d+)\"[ \t]*", lines[0])
    return match.group(1) if match else None


def toolchain_python(ctx: Context) -> Row:
    name = "toolchain"
    wanted = ci_python((ctx.root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    if wanted is None:
        return Row(
            UNKNOWN, name, "ci.yml doesn't name exactly one plain python-version", "Read .github/workflows/ci.yml"
        )
    ran = run([str(ctx.root / ".venv" / "bin" / "python"), "--version"], ctx.timeout, ctx.root)
    fix = f"uv sync --python {wanted}"
    if ran.outcome == "timeout":
        return Row(UNKNOWN, name, ".venv's Python gave no answer", fix)
    if ran.outcome == "missing" or ran.code != 0:
        return Row(FAIL, name, ".venv has no working Python", fix)
    match = re.search(r"Python (\d+\.\d+)(?!\d)", ran.out + ran.err)
    if not match:
        return Row(UNKNOWN, name, f".venv's Python version couldn't be read: {ran.out}", fix)
    if match.group(1) != wanted:
        return Row(FAIL, name, f".venv is Python {match.group(1)} and CI is {wanted}, so a local pass isn't CI's", fix)
    return Row(PASS, name, f"Python {wanted} matches CI")


def toolchain_lock(ctx: Context) -> Row:
    name = "toolchain"
    ran = run(["uv", "lock", "--check"], ctx.timeout, ctx.root)
    if ran.outcome == "missing":
        return Row(FAIL, name, "uv is not installed", "brew install uv")
    if ran.outcome == "timeout":
        return Row(UNKNOWN, name, f"uv lock --check gave no answer in {ctx.timeout:g}s", "Check the network")
    if ran.code == 0:
        return Row(PASS, name, "uv.lock is current")
    # It exits 1 for a stale lock and also when it can't resolve offline, so only the wording says which.
    text = ran.err + ran.out
    if "Unable to find lockfile" in text:
        return Row(FAIL, name, "uv.lock is missing", "uv lock")
    if "update the lockfile" in text or "needs to be updated" in text:
        return Row(FAIL, name, "uv.lock is out of date", "uv lock")
    last = text.strip().splitlines()[-1] if text.strip() else f"exit {ran.code}"
    return Row(UNKNOWN, name, f"uv lock --check couldn't tell: {last}", "Check the network, then run: uv lock --check")


def toolchain_ffmpeg(ctx: Context) -> Row:
    name = "toolchain"
    ran = run(["ffmpeg", "-version"], ctx.timeout, ctx.root)
    if ran.outcome == "timeout":
        return Row(UNKNOWN, name, "ffmpeg gave no answer", "Run: ffmpeg -version")
    if ran.outcome == "missing" or ran.code != 0:
        return Row(FAIL, name, "ffmpeg isn't installed or doesn't run", "brew install ffmpeg")
    return Row(PASS, name, "ffmpeg runs")


def check_toolchain(ctx: Context) -> Row:
    # Each guarded on its own, so a crash in one can't hide a definite failure in another.
    parts = [guarded("toolchain", part, ctx) for part in (toolchain_python, toolchain_lock, toolchain_ffmpeg)]
    for status in (FAIL, UNKNOWN):  # a FAIL outranks an UNKNOWN, and both name everything that isn't a pass
        if any(part.status == status for part in parts):
            bad = [part for part in parts if part.status != PASS]
            return Row(status, "toolchain", "; ".join(p.evidence for p in bad), "; ".join(p.fix for p in bad))
    return Row(PASS, "toolchain", "; ".join(part.evidence for part in parts))


# Not `${{`, which is a GitHub Actions expression, and whole words only, so AUTODOC isn't a TODO.
PLACEHOLDERS = re.compile(r"(?<!\$)\{\{|<your\b|\bTODO\b|\bPLACEHOLDER\b|\bREPLACE_ME\b")


def unquoted(value: str) -> str:
    value = value.strip()
    return value[1:-1].strip() if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'" else value


def description_is_empty(lines: list) -> bool:
    """`lines` is the frontmatter. A `>` or `|` marker holds nothing itself: its text is on the indented line after."""
    for index, line in enumerate(lines):
        match = re.fullmatch(r"description:[ \t]*(.*)", line)
        if not match:
            continue
        value = unquoted(match.group(1))
        if not re.fullmatch(r"[>|][+-]?\d*", value):
            return not value
        following = lines[index + 1] if index + 1 < len(lines) else ""
        return not (following[:1] in (" ", "\t") and following.strip())
    return True


def skill_problem(folder: Path) -> str | None:
    """What's wrong with one skill's SKILL.md, or None."""
    path = folder / "SKILL.md"
    if not path.is_file():
        return "no SKILL.md"
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---" or "---" not in [line.strip() for line in lines[1:]]:
        return "no frontmatter"
    closing = [line.strip() for line in lines[1:]].index("---") + 1
    fields = dict(
        match.groups()
        for match in (re.fullmatch(r"([A-Za-z-]+):[ \t]*(.*)", line) for line in lines[1:closing])
        if match
    )
    if unquoted(fields.get("name", "")) != folder.name:
        return f"name is {fields.get('name', 'missing')!r}, not its folder's"
    if description_is_empty(lines[1:closing]):
        return "no description"
    placeholder = PLACEHOLDERS.search(text)
    return f"still has the placeholder {placeholder.group()}" if placeholder else None


def check_skill_files(ctx: Context) -> Row:
    name = "skill files"
    skills = ctx.root / ".claude" / "skills"
    folders = sorted(path for path in skills.iterdir() if path.is_dir()) if skills.is_dir() else []
    if not folders:
        return Row(FAIL, name, "no skills found in .claude/skills", "Restore .claude/skills from main")
    problems = [(folder.name, skill_problem(folder)) for folder in folders]
    bad = [f"{skill}: {problem}" for skill, problem in problems if problem]
    if bad:
        return Row(FAIL, name, "; ".join(bad), "Fix each SKILL.md named here")
    return Row(PASS, name, f"{len(folders)} skills have valid frontmatter and no placeholders")


def check_ci(ctx: Context, remote: Row) -> Row:
    name = "ci"
    if remote.status != PASS:  # a green answer about another repo would say nothing about this commit
        return Row(UNKNOWN, name, "not asked, because origin isn't confirmed as this repo", "Fix the remote row first")
    head = run(["git", "rev-parse", "HEAD"], ctx.timeout, ctx.root)
    sha = head.out.strip()
    if head.outcome != "ok" or head.code != 0 or not sha:
        return Row(UNKNOWN, name, "couldn't read HEAD", "Run: git rev-parse HEAD")
    path = f"repos/{REPO}/commits/{sha}/check-runs?per_page=100"
    ran = run(["gh", "api", "--hostname", HOST, path], ctx.timeout, ctx.root)
    short = sha[:7]
    # ci.yml runs on pull requests and pushes to main, so pushing a branch alone never starts it.
    start_ci = "Push the commit and open a PR, then wait for CI and run --ci again"
    if ran.outcome == "missing":
        return Row(UNKNOWN, name, "gh is not installed", "brew install gh")
    if ran.outcome == "timeout":
        return Row(UNKNOWN, name, f"gh gave no answer in {ctx.timeout:g}s", "Check the network and run --ci again")
    if ran.code != 0:
        return Row(UNKNOWN, name, f"GitHub has no answer for {short}: {ran.err or ran.out}", start_ci)
    try:
        data = json.loads(ran.out)
        total, runs = data["total_count"], data["check_runs"]
        states = [(str(item["name"]), item["status"], item["conclusion"]) for item in runs]
    except (ValueError, KeyError, TypeError) as error:
        return Row(UNKNOWN, name, f"GitHub's answer couldn't be read: {type(error).__name__}", "Run --ci again")

    wait = "Wait for CI, then run --ci again"
    failed = [f"{n} ({c})" for n, s, c in states if s == "completed" and c not in ("success", "skipped", "neutral")]
    if failed:
        return Row(FAIL, name, f"{short}: {', '.join(failed)}", f"Read the log: gh run list --commit {sha}")
    if not states:
        return Row(UNKNOWN, name, f"{short} has no check runs, so nothing has tested it", start_ci)
    pending = [n for n, s, _ in states if s != "completed"]
    if pending:
        return Row(UNKNOWN, name, f"{short}: still running: {', '.join(pending)}", wait)
    if total > len(states):
        return Row(
            UNKNOWN, name, f"{short} has {total} check runs and only {len(states)} were read", "Read them on GitHub"
        )
    # `test` has to have really passed: on main the PR-only checks are skipped, which is fine, but all-skipped isn't.
    if not any(n == "test" and c == "success" for n, _, c in states):
        return Row(UNKNOWN, name, f"{short}: the test job didn't pass or didn't run", wait)
    dirty = run(["git", "status", "--porcelain"], ctx.timeout, ctx.root)
    unsure = dirty.outcome != "ok" or dirty.code != 0  # if it can't tell, it keeps the warning
    note = "; this covers the commit, not your uncommitted changes" if unsure or dirty.out.strip() else ""
    return Row(PASS, name, f"{short}: test passed and no check failed{note}")


CHECKS: list = [
    ("remote", check_remote),
    ("github access", check_github_access),
    ("git hook", check_git_hook),
    ("claude hooks", check_claude_hooks),
    ("toolchain", check_toolchain),
    ("skill files", check_skill_files),
]


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
    parser.add_argument("--ci", action="store_true", help="also check CI on HEAD")
    parser.add_argument("--timeout", type=float, default=20.0, help="seconds to allow each external call")
    args = parser.parse_args()

    ran = run(["git", "rev-parse", "--show-toplevel"], args.timeout)
    if ran.outcome == "timeout":
        print_row(
            Row(UNKNOWN, "remote", f"git gave no answer in {args.timeout:g}s", "Run: git rev-parse --show-toplevel")
        )
        return 2
    if ran.outcome == "missing":
        print_row(Row(FAIL, "remote", "git is not installed", "xcode-select --install"))
        return 1
    if ran.code != 0:
        print_row(Row(FAIL, "remote", "not inside a git repo", "Run /preflight from the repo"))
        return 1
    ctx = Context(Path(ran.out.strip()), args.timeout)

    rows = []
    for name, check in CHECKS:
        rows.append(guarded(name, check, ctx))
        print_row(rows[-1])
    if args.ci:
        rows.append(guarded("ci", lambda c: check_ci(c, rows[0]), ctx))
    else:  # a new local commit has no runs yet, so this would be UNKNOWN on most runs. The report says it's left out.
        rows.append(Row(SKIP, "ci", "not checked: pass --ci to check HEAD"))
    print_row(rows[-1])
    return exit_code(rows)


if __name__ == "__main__":
    sys.exit(main())
