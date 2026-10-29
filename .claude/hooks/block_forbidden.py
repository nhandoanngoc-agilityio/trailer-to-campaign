#!/usr/bin/env python3
"""PreToolUse hook: block Bash commands that break the "Never do" rules in CLAUDE.md.

Claude Code sends the pending tool call as JSON on stdin. Exiting with code 2
blocks the call, and whatever we print to stderr is shown to Claude as the reason.
Exit 0 lets the command run.

Blocked:
- rm with both recursive and force flags (rm -rf, rm -r -f, rm --recursive --force)
- git push --force / -f / +refspec   (--force-with-lease is allowed, per CLAUDE.md)
- git push to main, incl. HEAD:refs/heads/main (everything goes through a PR)
- git push / git push origin [HEAD|@] while on main, and git push --all/--mirror
- git commit while on main, incl. `git checkout main && git commit`
- git merge / cherry-pick / revert / rebase while on main (except --abort,
  --continue, --quit, --skip, and merge --ff-only)
- git reset --hard
- git add of a .env file (secrets come from the environment, never the repo)

Ported from the claude-training repo, where this hook is battle-tested.

Asked (Claude Code prompts the user before running):
- adding a dependency: uv add, uv pip install, pip install, python -m pip install

Commands are split on ; && || | & and wrappers like `sudo`, `env X=1`,
`bash -c "..."` are unwrapped, so `cd x && rm -rf y` is still caught.
Uses only the standard library, so no jq or extra dependency is needed.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys

SEPARATORS = {";", "&&", "||", "|", "&", "\n"}
WRAPPERS = {"sudo", "command", "exec", "nohup", "time", "env", "xargs"}
SHELLS = {"bash", "sh", "zsh"}
PROTECTED_BRANCH = "main"
HISTORY_COMMANDS = {"merge", "cherry-pick", "revert", "rebase"}
FINISH_FLAGS = {"--abort", "--continue", "--quit", "--skip"}
UV_VALUE_OPTIONS = {
    "--directory",
    "--project",
    "--config-file",
    "--cache-dir",
    "--color",
    "--python",
    "-p",
    "--allow-insecure-host",
}


def split_segments(command: str) -> list[list[str]]:
    """Split a shell command line into simple commands (lists of tokens)."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:  # unbalanced quotes: fall back to a rough split
        tokens = command.replace(";", " ; ").replace("&&", " && ").split()

    segments, current = [], []
    for tok in tokens:
        if tok in SEPARATORS or set(tok) <= set(";&|"):
            if current:
                segments.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


def unwrap(tokens: list[str]) -> list[str]:
    """Drop leading env assignments and wrappers like sudo/env."""
    while tokens:
        head = tokens[0]
        if "=" in head and not head.startswith("-") and head.split("=", 1)[0].isidentifier():
            tokens = tokens[1:]
        elif head in WRAPPERS:
            tokens = tokens[1:]
            while tokens and tokens[0].startswith("-"):
                tokens = tokens[1:]
        else:
            break
    return tokens


def current_branch(cwd: str | None) -> str | None:
    try:
        out = subprocess.run(
            ["git", "symbolic-ref", "--short", "-q", "HEAD"],  # works before the first commit too
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def check_rm(args: list[str]) -> str | None:
    recursive = force = False
    for arg in args:
        if arg == "--":
            break
        if arg in ("--recursive",):
            recursive = True
        elif arg == "--force":
            force = True
        elif arg.startswith("-") and not arg.startswith("--"):
            recursive |= "r" in arg or "R" in arg
            force |= "f" in arg
    if recursive and force:
        return "recursive force delete (rm -rf) is not allowed; delete specific files instead"
    return None


def switch_target(sub: str, rest: list[str]) -> str | None:
    """Branch that `git checkout`/`git switch` moves to, or None if it doesn't switch."""
    if "--" in rest:  # `git checkout main -- file` restores files, it doesn't switch
        return None
    for i, arg in enumerate(rest):
        if arg in ("-b", "-B", "-c", "-C", "--orphan") and i + 1 < len(rest):
            return rest[i + 1]
    positional = [a for a in rest if not a.startswith("-")]
    return positional[0] if positional else None


def push_destinations(rest: list[str]) -> list[str]:
    """Remote branch names a push writes to, e.g. HEAD:refs/heads/main -> main."""
    refspecs = [a for a in rest if not a.startswith("-")][1:]  # skip the remote name
    destinations = [r.lstrip("+").split(":")[-1].removeprefix("refs/heads/") for r in refspecs]
    return ["HEAD" if d == "@" else d for d in destinations]  # @ is git's alias for HEAD


def branch_in(state: dict, cwd: str | None) -> str | None:
    """Branch the current segment runs on: one switched to earlier in the command, else HEAD's."""
    if "branch" not in state:
        state["branch"] = current_branch(cwd)
    return state["branch"]


def skip_uv_globals(args: list[str]) -> list[str]:
    """Drop uv's global options so `uv --project . add x` is read as `uv add x`."""
    while args and args[0].startswith("-"):
        takes_value = args[0] in UV_VALUE_OPTIONS  # `--project=.` carries its value inline
        args = args[2:] if takes_value else args[1:]
    return args


def check_dependency(prog: str, args: list[str]) -> str | None:
    if prog == "uv":
        args = skip_uv_globals(args)
    adds = (
        (prog == "uv" and (args[:1] == ["add"] or args[:2] == ["pip", "install"]))
        or (re.fullmatch(r"pip[\d.]*", prog) and args[:1] == ["install"])
        or (re.fullmatch(r"python[\d.]*", prog) and args[:3] == ["-m", "pip", "install"])
    )
    if adds:
        return "this adds a dependency, and CLAUDE.md says to ask before adding a dependency or database"
    return None


def is_secret_file(arg: str) -> bool:
    """True for .env, .env.local, config/.env.prod ... but not .env.example."""
    name = arg.rsplit("/", 1)[-1]
    return (name == ".env" or name.startswith(".env.")) and not name.endswith(".example")


def check_git(args: list[str], cwd: str | None, state: dict) -> str | None:
    # skip global options such as `git -C dir` or `git -c key=val`
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-C", "-c") else 1
    if i >= len(args):
        return None
    sub, rest = args[i], args[i + 1 :]

    if sub == "push":
        if any(a in ("--force", "-f") or (a.startswith("-") and not a.startswith("--") and "f" in a) for a in rest):
            return "git push --force is not allowed; use --force-with-lease right after git pull --rebase"
        if any(a.startswith("+") for a in rest):
            return "force-pushing with a +refspec is not allowed"
        if any(a in ("--all", "--branches", "--mirror") for a in rest):
            return "git push --all/--mirror can update main; push one branch and open a PR"
        destinations = push_destinations(rest)
        if not destinations or "HEAD" in destinations:  # pushes the current branch
            branch = branch_in(state, cwd)
            destinations = [branch if d == "HEAD" else d for d in destinations] or [branch]
        if PROTECTED_BRANCH in destinations:
            return f"pushing to {PROTECTED_BRANCH} is not allowed; push a branch and open a PR"
    elif sub == "reset" and "--hard" in rest:
        return "git reset --hard discards work; use git stash or a new branch instead"
    elif sub == "add" and any(is_secret_file(a) for a in rest):
        return "secrets stay out of the repo; .env files are never staged"
    elif sub in ("checkout", "switch"):
        target = switch_target(sub, rest)
        if target:
            state["branch"] = target  # later segments in this command run on this branch
    elif sub == "commit":
        if branch_in(state, cwd) == PROTECTED_BRANCH:
            return f"committing directly on {PROTECTED_BRANCH} is not allowed; create a branch first (t<n>-<slug>)"
    elif sub in HISTORY_COMMANDS:
        finishing = any(a in FINISH_FLAGS for a in rest)
        fast_forward = sub == "merge" and "--ff-only" in rest
        if not finishing and not fast_forward and branch_in(state, cwd) == PROTECTED_BRANCH:
            return f"git {sub} on {PROTECTED_BRANCH} changes its history; do it on a branch and open a PR"
    return None


def find_violation(command: str, cwd: str | None = None, depth: int = 0, state: dict | None = None) -> str | None:
    """Return the reason a command breaks a rule, or None.

    `state` carries the branch across segments, so `git checkout main && git commit`
    is judged on main, not on whatever branch was checked out before the command.
    """
    state = {} if state is None else state
    if depth > 3:
        return None
    for segment in split_segments(command):
        tokens = unwrap(segment)
        if not tokens:
            continue
        prog = tokens[0].rsplit("/", 1)[-1]
        if prog in SHELLS and "-c" in tokens:
            idx = tokens.index("-c")
            if idx + 1 < len(tokens):
                reason = find_violation(tokens[idx + 1], cwd, depth + 1, state)
                if reason:
                    return reason
        elif prog == "rm":
            reason = check_rm(tokens[1:])
            if reason:
                return reason
        elif prog == "git":
            reason = check_git(tokens[1:], cwd, state)
            if reason:
                return reason
        else:
            ask = check_dependency(prog, tokens[1:])
            if ask:
                state.setdefault("ask", ask)  # main() asks only if nothing else blocks
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0  # never break Claude on malformed input
    command = (payload.get("tool_input") or {}).get("command") or ""
    state: dict = {}
    reason = find_violation(command, payload.get("cwd"), state=state)
    if reason:
        print(
            f"Blocked by team policy (CLAUDE.md 'Never do'): {reason}.\nCommand: {command}",
            file=sys.stderr,
        )
        return 2
    if "ask" in state:
        decision = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": f"Team policy: {state['ask']}.",
        }
        print(json.dumps({"hookSpecificOutput": decision}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
