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
`env X=1`, `bash -c "..."`, `eval`, shell keywords like `then`, and $(...) and
backticks are unwrapped, so `cd x && rm -rf y` is still caught.
Paths are read from the shell's directory (the payload's cwd, then any cd).
An expansion as the program or git's subcommand (`$x reset --hard`) can't be read,
so it's refused only when the words you can see already look forbidden.
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

WRAPPERS = {"sudo", "command", "exec", "nohup", "time", "env", "xargs", "caffeinate", "timeout", "gtimeout", "nice"}
WRAPPER_NUMBER = re.compile(r"[\d.]+[smhd]?")  # `timeout 5 x`, `nice -n 5 x`: the number isn't the program
KEYWORDS = {"if", "then", "elif", "else", "while", "until", "do", "{", "!"}  # a command follows each of these
SHELLS = {"bash", "sh", "zsh"}
MAX_NESTING = 5  # commands inside commands: bash -c, eval, $(...). Past this the command can't be judged, and blocks
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
# the next word a command, so scan() refuses them. &! and &| end a command, as & does.
OPERATOR = re.compile(r"&>>[|!]?|&>[|!]?|>>&[|!]?|>&[|!]?|>>[|!]?|>[|!]|&[!|]|<<<|<<|<>|<&|\|&|\|\||&&|;;|[;&|<>()\n]")
SEPARATOR_CHARS = set(";&|()\n")
REDIRECT_CHARS = set("<>&|")
GIT_PATH_WRITERS = {"checkout", "restore", "rm", "mv"}
GIT_LONG_VALUE_OPTIONS = {"--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix"}
GIT_JUDGED = HISTORY_COMMANDS | GIT_PATH_WRITERS | {"push", "reset", "add", "commit", "switch"}
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


class Op(str):
    """An unquoted shell operator (; && | > ( and so on), as opposed to the same text in quotes."""


class Nested(str):
    """A command that $(...) or backticks run, in parentheses or not: find_violation() judges it on its own."""


def read_substitution(command: str, opening: int, found: list[str]) -> int:
    """Add what the $(...) at opening runs to found, and return where it ends."""
    closing = scan(command, opening + 1, substitution=True)[1]
    found.append(Nested(command[opening + 1 : closing]))  # with its parentheses: a (( still reads as arithmetic
    return closing


def read_expanding_text(command: str, start: int, closing: str | None, found: list[str]) -> tuple[str, int]:
    """Text where only $(...) and backticks run, up to closing or the end: its text and where it stopped.

    That's a double-quoted string, or the body of a heredoc whose delimiter isn't quoted. What runs goes on found.
    """
    text = ""
    i, end = start, len(command)
    while i < end and command[i] != closing:
        if command[i] == "\\" and i + 1 < end and command[i + 1] in '$`"\\\n':  # a backslash only escapes these
            text += "" if command[i + 1] == "\n" else command[i + 1]
            i += 2
        elif command[i : i + 2] == "$(":
            stop = read_substitution(command, i, found)
            text += command[i:stop]
            i = stop
        elif command[i] == "`":  # runs to the next one not escaped, whatever quotes are inside
            stop = i + 1
            while stop < end and command[stop] != "`":
                stop += 2 if command[stop] == "\\" else 1
            if stop >= end:
                raise ValueError("a backtick left open")
            found.append(Nested(re.sub(r'\\([$`"\\])', r"\1", command[i + 1 : stop])))
            text += command[i : stop + 1]
            i = stop + 1
        else:
            text += command[i]
            i += 1
    return text, i


def read_heredoc_bodies(command: str, start: int, heredocs: list[tuple[str, bool, bool]], found: list[str]) -> int:
    """Where the command picks up after the heredoc bodies at start: they are text fed to a command, not commands.

    Unless the delimiter is unquoted: then a shell runs a body's $(...) and backticks, as in double quotes.
    One that never closes raises: a shell would read on to the end, but it's more likely a shift such as
    $[1<<2] or `let x=1<<2`, with commands after it.
    """
    i = start
    for delimiter, strip_tabs, expands in heredocs:
        body = i
        while True:
            if i >= len(command):
                raise ValueError(f"a heredoc that {delimiter} never closes")
            line_start = i
            newline = command.find("\n", i)
            stop = len(command) if newline == -1 else newline
            line = command[i:stop]
            i = stop + 1
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                break
        if expands:
            read_expanding_text(command[body:line_start], 0, None, found)
    return i


def scan(command: str, start: int = 0, substitution: bool = False) -> tuple[list[str], int]:
    """Read the command from start, by a shell's quoting rules: its tokens and where it stopped.

    Nested tokens hold what its $(...) and quoted backticks run, and the word around one keeps the text.
    With substitution=True, start is the ( of a $(...) and it stops past the matching ).

    What it can't follow raises, and main() blocks: a quote or a $(...) left open, unquoted backticks,
    a ${...} holding more than plain text, and inside $(...) a case, whose ) would look like the end of it,
    or a comment holding a ) or a quote, which macOS's bash and sh don't read as a comment. Refusing these
    is deliberate: each attempt to parse them opened new gaps (T24). It's a tripwire, not a shell.
    """
    tokens: list[str] = []
    word: str | None = None  # None between words: '' is an empty argument, and a word of its own
    heredocs: list[tuple[str, bool, bool]] = []  # delimiter, whether <<- lets it be indented, whether its body expands
    delimiter_at: tuple[int, bool] | None = None  # after << or <<-: where the delimiter word starts, and True for <<-
    arithmetic_at: int | None = None  # the depth a (( or x=( opened at: until its ), << is a shift, not a heredoc
    shifted = False  # a << read as a shift there. If its line ends before the ), it may be a heredoc, so it raises
    brackets = 0  # unquoted [ still open: `$[1<<3]`, `a[1<<3]=2`. A << there can't be told from `cat a[1<<EOF`...
    shifted_in_bracket = False  # ...so it's read as a shift, and the command is refused if another line follows
    depth = 0
    i, end = start, len(command)

    def finish_word() -> None:
        nonlocal word, delimiter_at
        if word is not None:
            if delimiter_at is not None:
                heredocs.append((word, delimiter_at[1], not re.search(r"['\"\\]", command[delimiter_at[0] : i])))
                delimiter_at = None
            elif substitution and word == "case" and starts_command():
                raise ValueError("a case inside $(...)")
            tokens.append(word)
        word = None

    def starts_command() -> bool:
        before = [t for t in tokens if not isinstance(t, Nested)]  # a substitution's first token is its (
        last = before[-1]
        timed = last == "time" or before[-2:] == ["time", "-p"]  # time is a keyword too, and takes a case
        return timed or (isinstance(last, Op) and set(last) <= SEPARATOR_CHARS) or last in KEYWORDS

    while i < end:
        char = command[i]
        operator = OPERATOR.match(command, i)
        if char in " \t":  # not \r: a shell reads it as part of a word
            finish_word()
            i += 1
        elif char == "#" and word is None:  # a comment runs to the end of the line
            newline = command.find("\n", i)
            stop = end if newline == -1 else newline
            if substitution and re.search(r"[)'\"`]", command[i:stop]):
                raise ValueError("a comment inside $(...) holding a ) or a quote")
            i = stop
        elif char == "$" and command[i + 1 : i + 2] == "{":  # plain ${...} is one word: no comment starts inside it
            closing = command.find("}", i)
            if closing == -1 or re.search(r"[\"'\\`$;&|<>(){\n]", command[i + 2 : closing]):
                raise ValueError("a ${...} holding more than plain text")
            word = (word or "") + command[i : closing + 1]
            i = closing + 1
        elif char == "$" and command[i + 1 : i + 2] == "(":  # it stays in the word: `$(echo git) reset` is one program
            closing = read_substitution(command, i, tokens)
            word = (word or "") + command[i:closing]
            i = closing
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
        elif char == '"':
            text, i = read_expanding_text(command, i + 1, '"', tokens)
            word = (word or "") + text
            if i >= end:
                raise ValueError('a " quote left open')
            i += 1
        elif char == "\\":
            if i + 1 < end and command[i + 1] != "\n":  # backslash-newline joins two lines
                word = (word or "") + command[i + 1]
            i += 2
        elif operator:
            opens_array = operator.group() == "(" and word is not None and word.endswith("=")
            finish_word()
            if "!" in operator.group() and operator.group() != "&!":
                raise ValueError(f"{operator.group()} means different things in zsh and bash")
            tokens.append(Op("&" if operator.group() in ("&!", "&|") else operator.group()))
            i = operator.end()
            delimiter_at = None
            if operator.group() == "\n":
                if shifted or shifted_in_bracket:
                    raise ValueError("a << that may be a shift, with its line ending before what closes it")
                i = read_heredoc_bodies(command, i, heredocs, tokens)
                heredocs = []
            elif operator.group() == "<<":
                if arithmetic_at is not None:
                    shifted = True
                elif brackets:
                    shifted_in_bracket = True
                else:
                    strip_tabs = command[i : i + 1] == "-"
                    i += 1 if strip_tabs else 0
                    delimiter_at = (i, strip_tabs)
            elif operator.group() == "(":
                if arithmetic_at is None and (opens_array or command[i : i + 1] == "("):
                    arithmetic_at = depth
                depth += 1
            elif operator.group() == ")":
                depth -= 1
                if depth == arithmetic_at:
                    arithmetic_at, shifted = None, False
                if substitution and depth == 0:
                    return tokens, i
        else:
            brackets = brackets + 1 if char == "[" else max(0, brackets - 1) if char == "]" else brackets
            word = (word or "") + char
            i += 1
    if substitution:
        raise ValueError("a $(...) left open")
    finish_word()
    return tokens, end


def split_segments(command: str) -> list[list[str]]:
    """Split a shell command line into simple commands (lists of tokens).

    Operators are split off as a shell does, unless quoted: so `a>b` is a redirect but
    `grep ">" f` is not, a newline separates commands, and `( )` group without hiding them.
    A Nested token is a segment of its own, ahead of its command: a shell runs it first.
    """
    segments, current = [], []
    for tok in scan(command)[0]:
        if isinstance(tok, Nested):
            segments.append([tok])
        # '' is an empty argument, not a separator; > >> &> >| are redirects, kept with their command
        elif isinstance(tok, Op) and set(tok) <= SEPARATOR_CHARS:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


def unwrap(tokens: list[str]) -> list[str]:
    """Drop leading env assignments, shell keywords and wrappers like sudo/env."""
    while tokens:
        head = tokens[0]
        if "=" in head and not head.startswith("-") and head.split("=", 1)[0].isidentifier():
            tokens = tokens[1:]
        elif head in KEYWORDS:
            tokens = tokens[1:]
        elif (
            head in WRAPPERS or (head[:1] == "/" and head.rsplit("/", 1)[-1] in WRAPPERS) or tokens[:2] == ["uv", "run"]
        ):
            tokens = tokens[2:] if head == "uv" else tokens[1:]
            while tokens and (tokens[0].startswith("-") or WRAPPER_NUMBER.fullmatch(tokens[0])):
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
        elif args[i] in GIT_LONG_VALUE_OPTIONS and GIT_JUDGED.isdisjoint(args[i + 1 : i + 2]):
            i += 2  # `--git-dir reset --hard` stays read as reset: git rejects it, but no block is lost for it
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


def is_expansion(word: str) -> bool:
    """True if the word may come, in part, from $x, ${x}, $(...) or backticks. Quotes are gone, so '$x' counts too."""
    return "$" in word or "`" in word


def is_expanded_program(word: str) -> bool:
    """True if the program's name is unknown. `"$root/bin/tool"` is a program called tool; `$(echo /bin/git)` isn't."""
    name = word.rsplit("/", 1)[-1]
    return is_expansion(name) or ("$" in word and bool(set(name) & set(")}")))


def check_rm_options(args: list[str]) -> str | None:
    """check_rm() on the options args start with, if they're all rm's own: `"$CC" -O2 -fPIC` and `-m x -rf` aren't."""
    options = []
    for arg in args:
        if arg in ("--recursive", "--force") or re.fullmatch(r"-[dfiIPRrvWx]+", arg):
            options.append(arg)
        elif arg.startswith("-") and arg not in ("-", "--"):
            return None
        else:
            break
    return check_rm(options)


def check_expanded_program(args: list[str], cwd: str | None, state: dict) -> str | None:
    """`$x reset --hard`: what $x holds can't be known, so refuse only what its visible arguments would be as git or rm.

    A copy of the state, because `$x checkout main` says nothing about the branch if $x isn't git. And `$x -c '...'`
    isn't read as a shell: `"$py" -c` is far more likely, and its code isn't shell.
    """
    probe = dict(state)
    sub, rest, _ = git_subcommand(args)
    # Two words unknown, as in `"$py" "$script" --all`: the second is likely a file, so only --hard looks forbidden.
    git_args = ["reset", *rest] if sub is not None and is_expansion(sub) else args
    return check_git(git_args, cwd, probe) or check_answer_key("git", args, probe) or check_rm_options(args)


def check_git(args: list[str], cwd: str | None, state: dict) -> str | None:
    sub, rest, _ = git_subcommand(args)  # past global options such as `git -C dir` or `git -c key=val`
    if sub is None:
        return None

    if is_expansion(sub):  # `git ${x:-reset} --hard`: would the visible words be refused under what it might hold?
        # --all, --branches and clusters like -Sfoo are ordinary under other subcommands: they aren't read as a push's
        words = [a for a in rest if a not in ("--all", "--branches") and (a == "-f" or not is_short_cluster(a))]
        for probe in ("reset", "push", "add"):
            reason = check_git(
                [probe, *words], cwd, {"branch": None}
            )  # no branch: a bare `git $x` isn't a push to main
            if reason:
                return reason
    elif sub == "push":
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


def shell_command_string(args: list[str]) -> str | None:
    """What a shell's -c runs: the first word that isn't an option, past a -c that may be clustered (-lc, -ec).

    A cluster counts only before the first positional. Past it, in `bash run.sh -vc x`, the script's own options
    are read only for an exact -c, as they always were.
    """
    start, takes_value = None, False
    for i, arg in enumerate(args):
        if takes_value:
            takes_value = False
        elif is_short_cluster(arg) and "c" in arg[1:]:
            start = i
            break
        elif arg[:1] in ("-", "+") and len(arg) > 1:
            takes_value = arg in ("--rcfile", "--init-file") or (arg.endswith(("o", "O")) and not arg.startswith("--"))
        else:
            break
    if start is None:
        start = args.index("-c") if "-c" in args else None
    if start is None:
        return None
    takes_value = args[start].endswith(("o", "O"))  # -o pipefail, or -eo pipefail
    for i, arg in enumerate(args[start + 1 :], start + 1):
        if takes_value:
            takes_value = False
        elif arg == "--":
            return args[i + 1] if i + 1 < len(args) else None
        elif arg[:1] in ("-", "+") and len(arg) > 1:
            takes_value = arg.endswith(("o", "O")) and not arg.startswith("--")
        else:
            return arg
    return None


def find_in_subshell(command: str, cwd: str | None, depth: int, state: dict) -> str | None:
    """Judge a command another shell runs: a cd inside it ends with that shell."""
    here = state["cwd"]
    reason = find_violation(command, cwd, depth + 1, state)
    state["cwd"] = here
    return reason


def find_violation(command: str, cwd: str | None = None, depth: int = 0, state: dict | None = None) -> str | None:
    """Return the reason a command breaks a rule, or None.

    `state` carries the branch across segments, so `git checkout main && git commit`
    is judged on main, not on whatever branch was checked out before the command.
    """
    state = {} if state is None else state
    # Paths are read from the shell's own directory, which carries over between Claude's Bash calls.
    state.setdefault("start", cwd or "")
    state.setdefault("cwd", cwd or "")
    if depth > MAX_NESTING:
        raise ValueError("commands nested too deeply to read")
    for segment in split_segments(command):
        if isinstance(segment[0], Nested):
            reason = find_in_subshell(segment[0], cwd, depth, state)
            if reason:
                return reason
            continue
        if any(tok.split("=", 1)[0] == ANSWER_KEY_OVERRIDE for tok in segment):
            return f"{ANSWER_KEY_OVERRIDE} is for a person changing the Answer Key on purpose, never an agent"
        words, targets = strip_redirects(segment)
        if any(in_answer_key(target, state) for target in targets):
            return ANSWER_KEY_REASON
        tokens = unwrap(words)
        after_expansion = False
        while tokens and is_expanded_program(tokens[0]):  # it may be a wrapper too, as in `$SUDO git reset --hard`
            reason = check_expanded_program(tokens[1:], cwd, state)
            if reason:
                return reason
            tokens = unwrap(tokens[1:])
            after_expansion = True
        if not tokens:
            continue
        prog = tokens[0].rsplit("/", 1)[-1]
        reason = check_answer_key(prog, tokens[1:], state)
        if reason:
            return reason
        if prog in SHELLS:
            string = shell_command_string(tokens[1:])
            reason = find_in_subshell(string, cwd, depth, state) if string is not None else None
            if reason:
                return reason
        # the shell's own eval: it runs its arguments, joined, in this shell, so a cd inside it carries on.
        # Not ./eval, and not `"$t2c" eval`, a subcommand of something unknown.
        elif tokens[0] == "eval" and not after_expansion:
            reason = find_violation(
                " ".join(tokens[2:] if tokens[1:2] == ["--"] else tokens[1:]), cwd, depth + 1, state
            )
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
