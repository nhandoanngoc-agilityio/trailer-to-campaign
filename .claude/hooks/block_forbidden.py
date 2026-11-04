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
- shell writes into the Answer Key, evals/expected/ (human-owned): > >> &> >| redirects,
  touch, tee, sed -i, cp into it (cp out of it is reading), and rm, mv or git checkout /
  restore / rm / mv of it, of anything in it, or of a folder that holds it (rm -r evals,
  git checkout .). Paths count with ./, absolute, after a cd, or from git -C. A tripwire
  for accidents, not a sandbox: a script or python -c that writes the file is out of
  reach: .githooks/pre-commit and the answer-key CI job catch those (T14).
- setting T2C_ANSWER_KEY_EDIT, the pre-commit override that only a person may use

Ported from the claude-training repo, where this hook is battle-tested.

Asked (Claude Code prompts the user before running):
- adding a dependency: uv add, uv pip install, pip install, python -m pip install

Commands are split on ; && || | & ( ) and newlines, and redirects are read
apart from the words, all as a shell does unless quoted. Wrappers like `sudo`,
`env X=1`, `bash -c "..."` are unwrapped, so `cd x && rm -rf y` is still caught.
Paths are read from the shell's directory (the payload's cwd, then any cd).
Uses only the standard library, so no jq or extra dependency is needed.

Runs on the machine's python3, not the project's, and macOS ships 3.9, so the hooks must
stay 3.9-compatible. Only exit 2 blocks, and a crash exits 1, which would let the command
run. So a failure while judging a command blocks it here, and `|| exit 2` in
.claude/settings.json blocks whatever never reaches that code, such as no python3 (T24).
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
import sys

WRAPPERS = {"sudo", "command", "exec", "nohup", "time", "env", "xargs"}
SHELLS = {"bash", "sh", "zsh"}
PROTECTED_BRANCH = "main"
HISTORY_COMMANDS = {"merge", "cherry-pick", "revert", "rebase"}
FINISH_FLAGS = {"--abort", "--continue", "--quit", "--skip"}
ANSWER_KEY = "evals/expected"
ANSWER_KEY_REASON = (
    "the Answer Key (evals/expected/) is human-owned: change the code or the prompt, never the expected answers"
)
ANSWER_KEY_OVERRIDE = "T2C_ANSWER_KEY_EDIT"  # lets .githooks/pre-commit take an Answer Key change from a person
# Split off as their own tokens, as a shell does, unless quoted. Longest first; ( and ) never join another.
# The ! forms and &| are zsh's, the Mac's shell. >! overwrites a file there, but in bash ! is the file and
# the next word a command, so tokenize() refuses them. &! and &| end a command, as & does.
OPERATOR = re.compile(r"&>>[|!]?|&>[|!]?|>>&[|!]?|>&[|!]?|>>[|!]?|>[|!]|&[!|]|<<<|<<|<>|<&|\|&|\|\||&&|;;|[;&|<>()\n]")
SEPARATOR_CHARS = set(";&|()\n")
REDIRECT_CHARS = set("<>&|")
GIT_PATH_WRITERS = {"checkout", "restore", "rm", "mv"}
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


HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")  # <<EOF, <<'EOF', <<-EOF; not <<<


def drop_heredoc_bodies(command: str) -> str:
    """The command without its heredoc bodies: they are text fed to a command, not commands."""
    kept, closing = [], []
    for line in command.split("\n"):
        if closing:  # inside a body: skip to its delimiter line
            if line.strip() == closing[0]:
                closing.pop(0)
            continue
        kept.append(line)
        closing = [m.group(2) for m in HEREDOC.finditer(line)]
    return "\n".join(kept)


class Op(str):
    """An unquoted shell operator (; && | > ( and so on), as opposed to the same text in quotes."""


def tokenize(command: str) -> list[str]:
    """The command's words, with its unquoted operators as Op tokens, read by a shell's quoting rules.

    shlex can't do this: it raises on `"\\""`, which a shell reads fine, and joins `)>` into one
    operator (T24). What it can't follow raises, and main() blocks: a quote left open, unquoted
    backticks, a ${...} holding more than plain text. Refusing these is deliberate: each attempt
    to parse them opened new gaps. It's a tripwire, not a shell: it doesn't look inside $(...)
    or backticks in double quotes (T27).
    """
    tokens: list[str] = []
    word: str | None = None  # None between words: '' is an empty argument, and a word of its own
    i, end = 0, len(command)

    def finish_word() -> None:
        nonlocal word
        if word is not None:
            tokens.append(word)
        word = None

    while i < end:
        char = command[i]
        operator = OPERATOR.match(command, i)
        if char in " \t":  # not \r: a shell reads it as part of a word
            finish_word()
            i += 1
        elif char == "#" and word is None:  # a comment runs to the end of the line
            newline = command.find("\n", i)
            i = end if newline == -1 else newline
        elif char == "$" and command[i + 1 : i + 2] == "{":  # plain ${...} is one word: no comment starts inside it
            closing = command.find("}", i)
            if closing == -1 or re.search(r"[\"'\\`$;&|<>(){\n]", command[i + 2 : closing]):
                raise ValueError("a ${...} holding more than plain text")
            word = (word or "") + command[i : closing + 1]
            i = closing + 1
        elif char == "`":  # what runs inside them, and how they join the words around them, can't be judged here
            raise ValueError("unquoted backticks")
        elif char == "$" and command[i + 1 : i + 2] == "'":  # $'...': a backslash escapes anything, even a quote
            closing = i + 2
            while closing < end and command[closing] != "'":
                closing += 2 if command[closing] == "\\" else 1
            if closing >= end:
                raise ValueError("a $' quote left open")
            if re.search(r"\\[^']", command[i + 2 : closing]):  # \n, \x67: what it decodes to can't be judged here
                raise ValueError("an escape in a $' quote")
            word = (word or "") + command[i + 2 : closing].replace("\\'", "'")
            i = closing + 1
        elif char == "'":  # nothing is special inside single quotes
            closing = command.find("'", i + 1)
            if closing == -1:
                raise ValueError("a ' quote left open")
            word = (word or "") + command[i + 1 : closing]
            i = closing + 1
        elif char == '"':  # a backslash only escapes these inside double quotes
            word = word or ""
            i += 1
            while i < end and command[i] != '"':
                if command[i] == "\\" and i + 1 < end and command[i + 1] in '$`"\\\n':
                    word += "" if command[i + 1] == "\n" else command[i + 1]
                    i += 2
                else:
                    word += command[i]
                    i += 1
            if i >= end:
                raise ValueError('a " quote left open')
            i += 1
        elif char == "\\":
            if i + 1 < end and command[i + 1] != "\n":  # backslash-newline joins two lines
                word = (word or "") + command[i + 1]
            i += 2
        elif operator:
            finish_word()
            if "!" in operator.group() and operator.group() != "&!":
                raise ValueError(f"{operator.group()} means different things in zsh and bash")
            tokens.append(Op("&" if operator.group() in ("&!", "&|") else operator.group()))
            i = operator.end()
        else:
            word = (word or "") + char
            i += 1
    finish_word()
    return tokens


def split_segments(command: str) -> list[list[str]]:
    """Split a shell command line into simple commands (lists of tokens).

    Operators are split off as a shell does, unless quoted: so `a>b` is a redirect but
    `grep ">" f` is not, a newline separates commands, and `( )` group without hiding them.
    """
    segments, current = [], []
    for tok in tokenize(drop_heredoc_bodies(command)):
        # '' is an empty argument, not a separator; > >> &> >| are redirects, kept with their command
        if isinstance(tok, Op) and set(tok) <= SEPARATOR_CHARS:
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


def strip_redirects(tokens: list[str]) -> tuple[list[str], list[str]]:
    """A command's own words, and the files its >, >>, &>, >| redirects write to."""
    words, targets = [], []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if isinstance(tok, Op) and set(tok) <= REDIRECT_CHARS:  # a redirect: it and its operand aren't the words
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            if ">" in tok:
                if words and words[-1].isdigit():  # the 2 in 2> is a stream number
                    words.pop()
                if not (tok.endswith("&") and (target.isdigit() or target == "-")):  # 2>&1 writes no file
                    targets.append(target)
            i += 2
            continue
        words.append(tok)
        i += 1
    return words, targets


def run_git(args: list[str], cwd: str | None) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=cwd or None, capture_output=True, text=True, timeout=3)
    except OSError:  # no git, or a folder the command has yet to make: git has nothing to say here
        return None
    # A timeout is left to raise: a hung git isn't "not on main", and main() blocks on it
    return out.stdout.strip() if out.returncode == 0 else None


def current_branch(cwd: str | None) -> str | None:
    return run_git(["symbolic-ref", "--short", "-q", "HEAD"], cwd)  # works before the first commit too


def git_subcommand(args: list[str]) -> tuple[str | None, list[str], list[str]]:
    """git's subcommand and its arguments, past global options, and the -C directories given."""
    i, dirs = 0, []
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("-C", "-c"):
            if args[i] == "-C" and i + 1 < len(args):
                dirs.append(args[i + 1])
            i += 2
        else:
            i += 1
    if i >= len(args):
        return None, [], dirs
    return args[i], args[i + 1 :], dirs


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


def resolve(path: str, base: str) -> str:
    """A path as text, read from base; absolute paths have their symlinks resolved (macOS /var is /private/var)."""
    full = posixpath.normpath(path if path.startswith("/") else posixpath.join(base, path))
    return os.path.realpath(full) if full.startswith("/") else full


def answer_key_path(state: dict) -> str:
    """Where the Answer Key is: under the repo the session is in, else under its directory."""
    if "answer_key" not in state:
        start = state.get("start") or None
        root = run_git(["rev-parse", "--show-toplevel"], start) or start or ""
        state["answer_key"] = resolve(ANSWER_KEY, root)
    return state["answer_key"]


def in_answer_key(path: str, state: dict, whole: bool = False) -> bool:
    """True if writing to path changes the Answer Key.

    By the path's text, read from where the shell is: a path in evals/expected/, from this
    repo or any clone. With whole=True (rm, mv, git checkout), a folder that holds the
    Answer Key counts too, since removing or restoring it takes the Answer Key with it.
    """
    if not path:
        return False
    full = resolve(path, state.get("cwd", ""))
    if full == ANSWER_KEY or full.startswith(ANSWER_KEY + "/") or full.endswith("/" + ANSWER_KEY):
        return True
    if f"/{ANSWER_KEY}/" in full:
        return True
    if whole:
        key = answer_key_path(state)
        return full in (".", "") or key.startswith(full.rstrip("/") + "/")
    return False


def positionals(args: list[str]) -> list[str]:
    """The arguments that aren't options (everything after -- counts)."""
    out, ended = [], False
    for arg in args:
        if ended or not arg.startswith("-") or arg == "-":
            out.append(arg)
        elif arg == "--":
            ended = True
    return out


def is_short_cluster(arg: str) -> bool:
    return arg.startswith("-") and not arg.startswith("--") and len(arg) > 1


def copy_destination(args: list[str]) -> list[str]:
    """Where cp/mv write: -t DIR (also in a cluster like -at), --target-directory=DIR, else the last positional."""
    for i, arg in enumerate(args):
        if arg.startswith("--target-directory="):
            return [arg.split("=", 1)[1]]
        if is_short_cluster(arg) and "t" in arg[1:]:
            attached = arg[arg.index("t", 1) + 1 :]
            if attached or i + 1 < len(args):
                return [attached or args[i + 1]]
    found = positionals(args)
    return found[-1:] if len(found) > 1 else []


def sed_in_place(args: list[str]) -> bool:
    return any(a.startswith("--in-place") or (is_short_cluster(a) and "i" in a[1:]) for a in args)


def check_answer_key(prog: str, args: list[str], state: dict) -> str | None:
    if prog == "cd":  # later segments in this command run from there
        target = positionals(args)[:1]
        state["cwd"] = resolve(target[0], state.get("cwd", "")) if target and target[0] not in ("-", "~") else ""
        return None
    written: list[str] = []  # paths written inside
    taken: list[str] = []  # paths removed, moved or restored whole
    where = state
    if prog in ("touch", "tee"):
        written = positionals(args)
    elif prog == "rm":
        taken = positionals(args)
    elif prog == "cp":
        written = copy_destination(args)
    elif prog == "mv":  # moving out of the Answer Key changes it too
        taken = positionals(args) + copy_destination(args)
    elif prog == "sed" and sed_in_place(args):
        written = positionals(args)
    elif prog == "git":
        sub, rest, dirs = git_subcommand(args)
        if sub in GIT_PATH_WRITERS:
            base = state.get("cwd", "")
            for d in dirs:  # git -C dir: its paths are read from there
                base = resolve(d, base)
            where = {**state, "cwd": base}
            taken = positionals(rest)
    if any(in_answer_key(p, where) for p in written) or any(in_answer_key(p, where, whole=True) for p in taken):
        return ANSWER_KEY_REASON
    return None


def is_secret_file(arg: str) -> bool:
    """True for .env, .env.local, config/.env.prod ... but not .env.example."""
    name = arg.rsplit("/", 1)[-1]
    return (name == ".env" or name.startswith(".env.")) and not name.endswith(".example")


def check_git(args: list[str], cwd: str | None, state: dict) -> str | None:
    sub, rest, _ = git_subcommand(args)  # past global options such as `git -C dir` or `git -c key=val`
    if sub is None:
        return None

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
    # Paths are read from the shell's own directory, which carries over between Claude's Bash calls.
    state.setdefault("start", cwd or "")
    state.setdefault("cwd", cwd or "")
    if depth > 3:
        return None
    for segment in split_segments(command):
        if any(tok.split("=", 1)[0] == ANSWER_KEY_OVERRIDE for tok in segment):
            return f"{ANSWER_KEY_OVERRIDE} is for a person changing the Answer Key on purpose, never an agent"
        words, targets = strip_redirects(segment)
        if any(in_answer_key(target, state) for target in targets):
            return ANSWER_KEY_REASON
        tokens = unwrap(words)
        if not tokens:
            continue
        prog = tokens[0].rsplit("/", 1)[-1]
        reason = check_answer_key(prog, tokens[1:], state)
        if reason:
            return reason
        if prog in SHELLS and "-c" in tokens:
            idx = tokens.index("-c")
            if idx + 1 < len(tokens):
                here = state["cwd"]
                reason = find_violation(tokens[idx + 1], cwd, depth + 1, state)
                state["cwd"] = here  # a cd inside bash -c ends with that shell
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
    command = ""
    state: dict = {}
    try:
        command = payload["tool_input"]["command"]  # this hook only sees Bash: no command means the payload changed
        if not isinstance(command, str) or not command:
            raise ValueError("the payload has no command")
        reason = find_violation(command, payload.get("cwd"), state=state)
    except Exception as error:  # a guard that crashes must not wave the command through
        print(
            f"Blocked: the guardrail hook failed ({error!r}), so this command wasn't checked. "
            f"Stop and ask a person; don't edit the hook to get past it.\nCommand: {command}",
            file=sys.stderr,
        )
        return 2
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
