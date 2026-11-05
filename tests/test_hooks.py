"""Tests for the Claude Code hooks in .claude/hooks/.

Each hook reads a tool call as JSON on stdin, the way Claude Code sends it.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / ".claude" / "hooks"


def hook_python() -> str:
    """The `python3` .claude/settings.json would find: not this project's virtualenv, which `uv run` puts first (T24).

    T2C_HOOK_PYTHON names another one, so CI can test the oldest we expect.
    """
    if override := os.environ.get("T2C_HOOK_PYTHON"):
        return override
    venv_bin = (Path(sys.prefix) / "bin").resolve()
    path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if Path(p).resolve() != venv_bin)
    found = shutil.which("python3", path=path)
    if not found:  # falling back to the project's Python would hide the very thing these tests are for
        raise RuntimeError("no python3 outside the virtualenv: set T2C_HOOK_PYTHON to test the hooks")
    return found


def run_hook(name: str, tool_input: dict, cwd: Path | None = None) -> subprocess.CompletedProcess:
    payload = {"tool_input": tool_input, "cwd": str(cwd) if cwd else None}
    return subprocess.run(
        [hook_python(), str(HOOKS / name)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo on a feature branch."""
    subprocess.run(["git", "init", "-q", "-b", "t1-skeleton", str(tmp_path)], check=True)
    return tmp_path


def git_switch_main(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "main"], check=True)


# --- block_forbidden.py -------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf build",
        "cd src && rm -r -f cache",
        "git push --force origin t1-skeleton",
        "git push origin HEAD:refs/heads/main",
        "git push origin +t1-skeleton",
        "git reset --hard HEAD~1",
        "git add .env",
        "git add config/.env.local",
        "bash -c 'git push -f'",
        "rm -r '' -f build",  # an empty argument is not a separator (found in T14)
        "git status\ngit push -f origin t1-skeleton",  # a newline separates commands (T14 /code-review)
    ],
)
def test_blocks_forbidden_commands(repo: Path, command: str) -> None:
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 2, result.stderr
    assert "Blocked by team policy" in result.stderr


@pytest.mark.parametrize(
    "command",
    [
        "git push -u origin t1-skeleton",
        "git push --force-with-lease origin t1-skeleton",
        "git add .env.example",
        "git add src/ tests/",
        "rm -r old_frames",
        "uv run pytest -q",
    ],
)
def test_allows_normal_commands(repo: Path, command: str) -> None:
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 0, result.stderr
    assert "permissionDecision" not in result.stdout


# T14: the Answer Key (evals/expected/) is human-owned. A tripwire for obvious shell writes, not a sandbox.

ANSWER_KEY_WRITES = [
    "echo x > evals/expected/charge.toml",
    "echo x >evals/expected/charge.toml",
    "echo x >> evals/expected/charge.toml",
    "uv run t2c eval 2> evals/expected/log.txt",
    "uv run t2c eval &> evals/expected/log.txt",
    "cp notes.toml evals/expected/charge.toml",
    "cp notes.toml evals/expected/",
    "cp -t evals/expected notes.toml",
    "cp --target-directory=evals/expected notes.toml",
    "mv evals/expected/charge.toml /tmp/",
    "mv notes.toml evals/expected/",
    "cat notes.toml | tee evals/expected/charge.toml",
    "tee -a evals/expected/charge.toml < notes.toml",
    "rm evals/expected/charge.toml",
    "touch evals/expected/new.toml",
    "sed -i 's/a/b/' evals/expected/charge.toml",
    "sed -i.bak 's/a/b/' evals/expected/charge.toml",
    "sed -i '' 's/a/b/' evals/expected/charge.toml",
    "sed --in-place 's/a/b/' evals/expected/charge.toml",
    "git checkout -- evals/expected/charge.toml",
    "git checkout main evals/expected/",
    "git restore evals/expected/charge.toml",
    "git rm evals/expected/charge.toml",
    "git mv evals/expected/charge.toml evals/expected/old.toml",
    # the same writes, spelled differently
    "touch ./evals/expected/new.toml",
    "touch /Users/someone/trailer-to-campaign/evals/expected/new.toml",
    "cd evals/expected && touch new.toml",
    "cd evals && sed -i 's/a/b/' expected/charge.toml",
    "bash -c 'cp notes.toml evals/expected/charge.toml'",
    "sudo rm evals/expected/charge.toml",
    # from /code-review on #45
    "echo hi\ntouch evals/expected/x.toml",
    "echo hi>evals/expected/x.toml",
    "cat a>>evals/expected/b.toml",
    "echo x >| evals/expected/a.toml",
    "rm -r evals",
    "git checkout -- evals",
    "git checkout .",
    "git restore .",
    "mv evals evals_old",
    "sed -Ei 's/a/b/' evals/expected/x.toml",
    "git -C evals checkout -- expected/a.toml",
    "cp -at evals/expected notes.toml",
    "(cd evals/expected && touch x.toml)",
    "cat > evals/expected/x.toml <<'EOF'\nfilms = []\nEOF",
    "cat <<'EOF' > notes.md\nhi\nEOF\ntouch evals/expected/x.toml",  # a command after the heredoc still counts
]

ANSWER_KEY_READS = [
    "cat evals/expected/charge.toml",
    "ls evals/expected/",
    "diff evals/expected/charge.toml /tmp/charge.toml",
    "grep spoiler evals/expected/charge.toml",
    "cp evals/expected/charge.toml /tmp/",
    "sed -n 1,5p evals/expected/charge.toml",
    "git diff -- evals/expected/",
    "git log -- evals/expected/",
    "git show HEAD:evals/expected/charge.toml > /tmp/charge.toml",
    "echo hi > evals/drafts/charge.toml",
    "cd evals/expected && cat charge.toml",
    # from /code-review on #45
    'grep ">" evals/expected/charge.toml',
    "bash -c 'cd evals/expected' && touch notes.toml",  # bash -c's cd ends with that shell
    "git checkout main",
    "git checkout -b t14-guard",
    "uv run t2c eval 2>&1 | tee /tmp/eval.log",
    "cp notes.toml backup.toml 2> /tmp/err.log",
    # a heredoc's body is text, not commands, even now that newlines separate commands
    "cat > notes.md <<'EOF'\nNever run git push --force or rm -rf build.\nEOF",
    "cat >> notes.md <<EOF\ntouch evals/expected/x.toml\nEOF\necho done",
]


@pytest.mark.parametrize("command", ANSWER_KEY_WRITES)
def test_blocks_shell_writes_to_the_answer_key(repo: Path, command: str) -> None:
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 2, result.stderr
    assert "Answer Key" in result.stderr


@pytest.mark.parametrize("command", ANSWER_KEY_READS)
def test_allows_reading_the_answer_key(repo: Path, command: str) -> None:
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("command", ["touch expected/x.toml", "sed -i 's/a/b/' expected/charge.toml", "rm -r expected"])
def test_a_shell_already_inside_evals_is_still_guarded(repo: Path, command: str) -> None:
    # Claude's Bash keeps its directory between calls: an earlier `cd evals` leaves the shell there.
    result = run_hook("block_forbidden.py", {"command": command}, repo / "evals")
    assert result.returncode == 2, result.stderr
    assert "Answer Key" in result.stderr


def test_blocks_commit_on_main(repo: Path) -> None:
    git_switch_main(repo)
    result = run_hook("block_forbidden.py", {"command": "git commit -m wip"}, repo)
    assert result.returncode == 2


def test_blocks_switching_to_main_then_committing(repo: Path) -> None:
    result = run_hook("block_forbidden.py", {"command": "git checkout main && git commit -m x"}, repo)
    assert result.returncode == 2


def test_asks_before_adding_a_dependency(repo: Path) -> None:
    result = run_hook("block_forbidden.py", {"command": "uv add anthropic"}, repo)
    assert result.returncode == 0
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "ask"


def test_ignores_malformed_input() -> None:
    result = subprocess.run(
        [hook_python(), str(HOOKS / "block_forbidden.py")], input="not json", capture_output=True, text=True
    )
    assert result.returncode == 0


def test_a_crash_while_judging_blocks_the_command() -> None:
    """Exit 1 lets a command run, so a guard that crashes would wave everything through (T24)."""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(HOOKS)!r})\n"
        "import block_forbidden as hook\n"
        "def crash(*args, **kwargs):\n"
        "    raise TypeError('zip() takes no keyword arguments')\n"
        "hook.find_violation = crash\n"
        "sys.exit(hook.main())\n"
    )
    result = subprocess.run(
        [hook_python(), "-B", "-c", script],
        input=json.dumps({"tool_input": {"command": "git status"}}),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "wasn't checked" in result.stderr
    assert "ask" in result.stderr  # a block means stop and ask, not edit the guard (CLAUDE.md)


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        "{}",
        "null",
        '{"tool_input": null}',
        '{"tool_input": {}}',
        '{"tool_input": {"cmd": "git push -f"}}',
        '{"tool_input": {"command": 1}}',
    ],
)
def test_a_payload_with_no_command_blocks(payload: str) -> None:
    """This hook only sees Bash calls, so no command means Claude Code's payload changed (T24)."""
    result = subprocess.run(
        [hook_python(), str(HOOKS / "block_forbidden.py")], input=payload, capture_output=True, text=True
    )
    assert result.returncode == 2


@pytest.mark.parametrize(
    "command",
    [
        'echo "\\""|git reset --hard',
        'echo "\\""||git push --force origin main',
        'echo "\\""&git reset --hard',
        '(git reset --hard;echo "\\"")',
        'echo "\\"">evals/expected/x.toml',
        '(echo "\\"")>evals/expected/x.toml',  # ")>" is a separator and a redirect, not one operator
        "(echo hi)>evals/expected/x.toml",  # the same, where shlex can read it
        "(echo hi)>>evals/expected/x.toml",
        'bash -c "git reset --hard"; echo "\\""',  # a quoted -c payload stays one word (security review)
        "cat <(git reset --hard)",
        "echo >(git reset --hard)",
        "echo hi;>evals/expected/x.toml",
        "echo hi|>evals/expected/x.toml",
        "git reset --hard # a comment with a stray ' quote",
        "echo $'\\'' ; git reset --hard",  # \' doesn't end a $'...' string (T24 @claude)
        "echo $'\\'' > evals/expected/x.toml",
        "echo 'never closed ; git reset --hard",  # a quote left open: the command can't be read, so it isn't checked
        'echo "never closed ; git reset --hard',
        "echo $'never closed ; git reset --hard",
        "echo hi >! evals/expected/x.toml",  # zsh's clobbering redirects, and zsh is the Mac's shell (security review)
        "echo hi >>! evals/expected/x.toml",
        "echo hi &>! evals/expected/x.toml",
        ">! git reset --hard",  # bash reads ! as the file and runs the rest; zsh doesn't, so >! can't be judged
        "bash -c '>! git reset --hard'",
        "echo } ${x:-a # b}; git reset --hard",  # a stray } mustn't hide an open ${
        "echo `echo a # b`; git reset --hard",  # no comment starts inside backticks
        "echo `git reset --hard`",  # and what's inside them runs
        "echo `true; git reset --hard`",
        "git `echo x` reset --hard",  # unquoted backticks can't be judged, wherever they sit
        "echo `date`#x; git reset --hard",
        "echo `echo \\`git reset --hard\\``",
        "echo ${x:-$(git reset --hard)}",  # so can't a ${...} holding more than plain text
        "echo ${x:-`git reset --hard`}",
        "echo ${x:-'}'; git reset --hard}",
        "echo ${x:-\\}}; git reset --hard # }",
        "echo ${x:-${y}}; git reset --hard # }",
        "bash -c $'echo\\ngit reset --hard'",  # $'...' escapes aren't decoded, so they can't be judged
        "bash -c $'\\x67it reset --hard'",
        "echo hi &! git reset --hard",  # zsh's &! and &| end a command as & does
        "echo hi &| git reset --hard",
        "echo a\r# ; git reset --hard",  # a shell reads \r as part of a word, so # starts no comment
        "echo ${x:-a # b}; git reset --hard",  # nor does # inside ${...}
    ],
)
def test_operators_still_split_a_command_shlex_cannot_read(repo: Path, command: str) -> None:
    """A shell reads "\\"" fine, but shlex raises, and the fallback must not hide what follows (T24 /code-review)."""
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 2, result.stderr


@pytest.mark.parametrize(
    "command",
    [
        'echo "a | git reset --hard" "\\""',  # operators inside quotes are text
        "echo 'a; git reset --hard'",
        'grep "\\"" notes.md',
        "echo $'a; git reset --hard'",
        "echo $'it\\'s'",
        "echo ${x:-a # b}",
        "echo ${x} # a comment after a variable",
        "for f in ${files}; do echo $f; done # note",
        'echo "`date`" # a comment after quoted backticks',  # what they run is judged on its own: T27
        "echo ${#files} ${x:-a b} ${y%.txt}",
        "echo git\\ reset\\ --hard",
        "echo hi # git reset --hard",
    ],
)
def test_quoted_text_is_not_a_command(repo: Path, command: str) -> None:
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 0, result.stderr


# T27: a forbidden command nested in other shell syntax is still that command. Both sides, per CLAUDE.md.

NESTED_FORBIDDEN = [
    # command substitution inside double quotes
    'echo "$(git reset --hard)"',
    'echo "a $(git reset --hard) b"',
    'x="$(git reset --hard)"',
    'echo "`git reset --hard`"',
    'echo "$(echo "$(git reset --hard)")"',
    'echo "${x:-$(git reset --hard)}"',
    "echo \"$(echo ')'; git reset --hard)\"",  # a ) in quotes doesn't close it
    'echo "$(# )\ngit reset --hard)"',  # nor does one in a comment
    'echo "$(case x in a) git reset --hard;; esac)"',  # a case's ) would close it early, so it's refused
    'echo "$(if true; then case x in a) git reset --hard;; esac; fi)"',
    "git commit -m \"$(cat <<'EOF'\nhi\nEOF\ngit reset --hard\n)\"",  # a command after the heredoc still counts
    'bash -c "echo \\"\\$(git reset --hard)\\""',
    # the heredoc pass reads quotes and comments now
    "echo '<<EOF'\ngit reset --hard",
    'echo "<<EOF"\ngit reset --hard',
    "# <<EOF\ngit reset --hard",
    "echo \\<<EOF\ngit reset --hard",
    "python3 -c 'x = 1 << y'\ngit reset --hard",
    # << is a shift in arithmetic, not a heredoc
    "echo $((1<<y))\ngit reset --hard",
    'echo "$((1<<y))"\ngit reset --hard',
    "((x<<y))\ngit reset --hard",
    # what looks like a heredoc but never closes can't be judged: $[...] and let shift too
    "echo $[1<<y]\ngit reset --hard",
    "let x=1<<y\ngit reset --hard",
    "cat <<EOF\ngit reset --hard",
    # commands after a real heredoc, however its delimiter is written
    "cat <<\\EOF\nhi\nEOF\ngit reset --hard",
    "cat <<END-OF-TEXT\nhi\nEND-OF-TEXT\ngit reset --hard",
    "cat <<EOF # note\nhi\nEOF\ngit reset --hard",
    "cat <<EOF; git reset --hard\nhi\nEOF",
    "cat <<A <<B\nhi\nA\nthere\nB\ngit reset --hard",
    "bash -c 'cat <<EOF\nhi\nEOF\ngit reset --hard'",  # the -c command is read on its own, past its heredoc
    # a branch switched to in a substitution is the branch the commit lands on
    'x="$(git checkout main)"; git commit -m hi',
    'git commit -m "$(git checkout main)"',
    # a shell runs $(...) and backticks in a heredoc's body, unless its delimiter is quoted
    "cat <<EOF\n$(git reset --hard)\nEOF",
    "cat <<EOF\n`git reset --hard`\nEOF",
    "cat <<-EOF\n\t$(git reset --hard)\n\tEOF",
    "cat <<A <<'B'\n$(git reset --hard)\nA\nhi\nB",
    "cat <<'A' <<B\nhi\nA\n$(git reset --hard)\nB",
    "cat <<EOF\n'$(git reset --hard)'\nEOF",  # single quotes are only text in a body
    'git commit -m "$(cat <<EOF\n$(git reset --hard)\nEOF\n)"',
    # bash also closes a heredoc at `EOF)` inside $(...), so it's refused, not read as a body
    'echo "$(cat <<EOF\nhi\nEOF)"; git reset --hard',
    # shell keywords aren't the program's name
    "if true; then git reset --hard; fi",
    "if git reset --hard; then echo hi; fi",
    "if true\nthen\n  git reset --hard\nfi",
    "if a; then b; else git reset --hard; fi",
    "if a; then b; elif git reset --hard; then c; fi",
    "while true; do git push -f origin t1-skeleton; done",
    "while git reset --hard; do break; done",
    "until git reset --hard; do break; done",
    "for f in a b; do rm -rf build; done",
    "{ git reset --hard; }",
    "! git reset --hard",
    "if true; then sudo git reset --hard; fi",
    "if true; then X=1 git reset --hard; fi",
    # eval runs its arguments as a command
    "eval 'git reset --hard'",
    'eval "git reset --hard"',
    "eval git reset --hard",
    "eval -- 'git reset --hard'",
    "eval \"bash -lc 'git reset --hard'\"",
    # -c clustered with other flags, and flags between it and the command
    "bash -lc 'git reset --hard'",
    "bash -cl 'git reset --hard'",
    "sh -ec 'git reset --hard'",
    "zsh -lc 'git reset --hard'",
    "bash -l -c 'git reset --hard'",
    "bash -c -l 'git reset --hard'",
    "bash -o pipefail -c 'git reset --hard'",
    "bash -c -o pipefail 'git reset --hard'",
    "bash -ceo pipefail 'git reset --hard'",
    "bash -c -- 'git reset --hard'",
    # an expansion as the program or subcommand, when what's visible already looks forbidden
    "${x:-git} reset --hard",
    "$x reset --hard",
    "git ${x:-reset} --hard",
    '"$x" reset --hard',
    "$(echo git) reset --hard",
    '"$(echo git)" reset --hard',
    '"`which git`" reset --hard',
    "git $(echo reset) --hard",
    'git "$x" --hard',
    "git -C repo $x --hard",
    "$x $y --hard",
    "sudo $x reset --hard",
    "if true; then $x reset --hard; fi",
    "$x push --force origin t1-skeleton",
    "$x push origin main",
    "$x add .env",
    "git $x --force origin t1-skeleton",
    "git $x origin main",
    "git $x .env",
    "$x -rf build",
    "$SUDO git reset --hard",
    "$SUDO rm -rf build",
]


@pytest.mark.parametrize("command", NESTED_FORBIDDEN)
def test_blocks_a_forbidden_command_nested_in_shell_syntax(repo: Path, command: str) -> None:
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 2, result.stderr


@pytest.mark.parametrize(
    "command",
    [
        'echo "$(cd evals/expected && touch x.toml)"',
        'echo "$(echo x > evals/expected/a.toml)"',
        "if true; then touch evals/expected/x.toml; fi",
        "eval 'touch evals/expected/x.toml'",
        "eval 'cd evals/expected'; touch x.toml",  # eval runs in this shell, so its cd carries on
        "bash -lc 'touch evals/expected/x.toml'",
        "$x checkout -- evals/expected/a.toml",
        "$x checkout .",
        "$x touch evals/expected/a.toml",
    ],
)
def test_blocks_a_nested_write_to_the_answer_key(repo: Path, command: str) -> None:
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 2, result.stderr
    assert "Answer Key" in result.stderr


def test_a_cd_in_a_substitution_ends_with_its_shell(repo: Path) -> None:
    command = 'x="$(cd evals && pwd)"; rm -r expected'  # this `expected` isn't the Answer Key's
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "wrap",
    [lambda c: f"bash -c {shlex.quote(c)}", lambda c: f"eval {shlex.quote(c)}", lambda c: f'echo "$({c})"'],
    ids=["bash -c", "eval", "substitution"],
)
def test_nesting_too_deep_to_read_blocks(repo: Path, wrap: Callable[[str], str]) -> None:
    """Past the limit the command can't be judged, so it isn't waved through."""
    command = "echo hi"
    for _ in range(8):
        command = wrap(command)
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 2, result.stderr
    assert "wasn't checked" in result.stderr


@pytest.mark.parametrize(
    "command",
    [
        # a real heredoc's body stays text, however its delimiter is written
        "git commit -m \"$(cat <<'EOF'\nNever run git reset --hard; it's bad (really).\nEOF\n)\"",
        "gh pr create --title x --body \"$(cat <<'EOF'\nrm -rf build\n`git reset --hard`\n$(git push -f)\nEOF\n)\"",
        "cat <<\\EOF\ngit reset --hard\nEOF",
        'cat <<"EOF"\ngit reset --hard\nEOF',
        "cat <<EOF\ngit reset --hard\nEOF",
        "cat <<-EOF\n\tgit reset --hard\n\tEOF",
        "cat <<- EOF\n\tgit reset --hard\n\tEOF",
        "cat <<END-OF-TEXT\nrm -rf build\nEND-OF-TEXT",
        "cat <<'---'\nrm -rf build\n---",
        "cat <<A <<B\ngit reset --hard\nA\nrm -rf build\nB",
        "cat <<EOF\n  EOF\ngit reset --hard\nEOF",  # an indented delimiter closes only <<-
        "python3 - <<'PY'\nprint(\"it's rm -rf build\")\nPY",
        "cat > notes.md <<'EOF'\nif true; then git reset --hard; fi\neval 'git reset --hard'\nEOF",
        "bash -c 'cat <<EOF\nrm -rf build\nEOF'",  # main blocked this: its heredoc pass broke the quote apart
        # a quoted delimiter keeps $(...) and backticks in the body as text, however it's quoted
        "cat <<'EOF'\n$(git reset --hard)\nEOF",
        'cat <<"EOF"\n`git reset --hard`\nEOF',
        "cat <<\\EOF\n$(git reset --hard)\nEOF",
        "cat <<E'O'F\n$(git reset --hard)\nEOF",
        # an unquoted one runs them, so what they run is judged, and an escaped one is text
        "cat <<EOF\nDate: $(date) on `hostname`\nEOF",
        "cat <<EOF\n\\$(git reset --hard)\nEOF",
        "cat <<EOF\n\\`git reset --hard\\`\nEOF",
        'cat <<EOF\nit\'s "quoted" (text) $x ${y}\nEOF',
        # quoted text and plain arguments aren't commands
        "echo '$(git reset --hard)'",
        'echo "\\$(git reset --hard)"',
        'echo "\\`git reset --hard\\`"',
        "echo 'then git reset --hard'",
        "echo then git reset --hard",
        "echo if git reset --hard",
        "echo eval git reset --hard",
        "echo bash -lc git reset --hard",
        "grep -r 'bash -lc' docs",
        'echo "$x" reset --hard',
        # what only looks like a heredoc, with commands after it
        "echo '<<EOF'\necho next",
        'echo "<<EOF"\necho next',
        "# <<EOF\necho next",
        "echo $((1<<2))\necho next",
        'echo "$((1<<2))"\necho next',
        "((x<<2))\necho next",
        "echo $[1<<2]",
        "let x=1<<2",
        "cat <<< 'git reset --hard'",
        # substitutions that do nothing forbidden
        'echo "$(git status)"',
        'x="$(git rev-parse --show-toplevel)"',
        'echo "`date`"',
        'echo "$(dirname "$(pwd)")"',
        "echo \"$(echo ')')\"",
        'echo "$(grep case notes.md)"',
        "echo $(date)",
        "echo $((1+2))",
        # keywords around allowed commands
        "if true; then git status; fi",
        'for f in a b; do echo "$f"; done',
        'while read -r line; do echo "$line"; done < notes.md',
        "{ echo a; echo b; } > notes.md",
        "! git diff --quiet",
        "case x in a) echo hi;; esac",
        # eval and shells around allowed commands
        "eval 'echo hi'",
        'eval "$x"',
        'eval "$(ssh-agent -s)"',
        "bash -lc 'uv run pytest -q'",
        "bash -ec 'echo hi'",
        "bash -l script.sh",
        "bash -x script.sh",
        "bash --norc script.sh",
        # an expansion that could be anything: normal commands stay allowed
        '"$py" --version',
        "$py --version",
        '"$py" -m pytest -q',
        "\"$py\" -c 'print(1)'",
        '"$py" commit.py',
        '"$py" reset.py --hard-mode',
        "${PYTHON:-python3} -m pytest -q",
        '"$(which python3)" --version',
        "$(git rev-parse --show-toplevel)/scripts/check.sh --all",
        "$HOME/bin/tool run",
        '"$root/.venv/bin/python" -m pytest',
        "$x",
        '"$x" status',
        "$x reset",
        "$x reset --soft",
        '"$pip" install -r requirements.txt -f links',
        '"$py" evals/expected/charge.toml',
        '"$EDITOR" evals/expected/charge.toml',
        'git "$@"',
        "git $x",
        "git $x --oneline",
        "git $x -- src/",
        'git -C "$dir" status',
        'git --git-dir "$dir" status',
        'git log --format="$x"',
    ],
)
def test_allows_what_only_looks_nested(repo: Path, command: str) -> None:
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 0, result.stderr
    assert "permissionDecision" not in result.stdout


def test_asks_before_a_dependency_added_in_a_nested_command(repo: Path) -> None:
    for command in ('echo "$(uv add anthropic)"', "if true; then uv add anthropic; fi", "eval 'uv add anthropic'"):
        result = run_hook("block_forbidden.py", {"command": command}, repo)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_an_expansion_doesnt_read_as_a_commit_off_main(repo: Path) -> None:
    result = run_hook("block_forbidden.py", {"command": "$x commit -m hi"}, repo)
    assert result.returncode == 0, result.stderr


def test_an_expansion_reads_as_a_commit_on_main(repo: Path) -> None:
    git_switch_main(repo)
    result = run_hook("block_forbidden.py", {"command": "$x commit -m hi"}, repo)
    assert result.returncode == 2, result.stderr


def test_an_unknown_subcommand_on_main_is_not_a_bare_push(repo: Path) -> None:
    git_switch_main(repo)
    for command in ("git $x", 'git "$@"', 'git --git-dir "$dir" status'):
        result = run_hook("block_forbidden.py", {"command": command}, repo)
        assert result.returncode == 0, result.stderr


def test_an_expansion_doesnt_carry_a_branch_switch(repo: Path) -> None:
    """`$x` may not be git, so `$x checkout main` says nothing about the branch the commit lands on."""
    result = run_hook("block_forbidden.py", {"command": "$x checkout main && git commit -m hi"}, repo)
    assert result.returncode == 0, result.stderr


# T27, review round 1. Each pair: what must still block, and what must stay allowed.

ROUND_1_FORBIDDEN = [
    # 1. macOS bash and sh close a $(...) at a ) in a comment, so what follows runs
    'echo "$(# )"; git reset --hard; echo "\n)"',
    'echo "$(# )"; rm -rf build; echo "\n)"',
    'echo "$(# )" && git push -f; echo "\n)"',
    'x="$(ls # list )"; git reset --hard; echo "\n)"',
    'bash -c \'echo "$(# )"; git reset --hard; echo "\n)"\'',
    "echo $(# )\ngit reset --hard\n)",
    # a quote in such a comment would open a quote for a shell that doesn't see the comment
    "echo \"$(# '\n')\"; git reset --hard; echo '\n)\"",
    # 2. what is still rm -rf through an expansion
    "$x -r -f build",
    "$x -fr build",
    "$x -rfv build",
    "$x --recursive --force build",
    "$x -rf -- build",
    # 3. value-taking git options, which used to hide the subcommand
    "git --git-dir .git reset --hard",
    "git --work-tree . reset --hard",
    "git --git-dir .git --work-tree . reset --hard",
    "git --namespace x push --force origin t1-skeleton",
    "$x --git-dir .git reset --hard",
    # main read these as the subcommand. git itself rejects them, but this PR keeps those blocks
    "git --git-dir reset --hard",
    "git --work-tree push origin main",
    "git --git-dir push -f",
    # a $(...) stays one word, and what follows it is still read
    "echo $(date); git reset --hard",
    "echo $(date) && rm -rf build",
    # 4. a path whose last part is literal is that program
    "\"$root/bin/bash\" -lc 'git reset --hard'",
    '"$HOME/bin/rm" build -rf',
    '"$dir/git" reset --hard',
    # and a path inside an expansion isn't: the program is still unknown
    "$(echo /usr/bin/git) reset --hard",
    '"$(echo /usr/bin/git)" reset --hard',
    "${x:-/usr/bin/git} reset --hard",
    '"`echo /usr/bin/git`" reset --hard',
    # 5. a heredoc after (( )) is a heredoc, so what follows it is commands
    "((x)); cat <<'EOF'\nit's text\nEOF\ngit reset --hard\n# it's done",
    "for ((i=0;i<3;i++)); do cat <<'EOF'\nit's text\nEOF\ndone\ngit reset --hard\n# it's done",
    "((a); cat <<'EOF'\nit's\nEOF\ngit reset --hard\n# it's\n)",  # (( as two subshells: can't be read as either
    # 6. a wrapper by its full path
    "/usr/bin/env bash -lc 'git reset --hard'",
    "/usr/bin/env git reset --hard",
    "/usr/bin/sudo git reset --hard",
    "/usr/bin/time git reset --hard",
    # the wrappers this project really uses, some with a number first
    "caffeinate -i git reset --hard",
    "caffeinate -t 3600 git reset --hard",
    "timeout 5 git reset --hard",
    "timeout -k 5 10s git reset --hard",
    "gtimeout 5 git reset --hard",
    "nice -n 5 git reset --hard",
    "xargs -n 1 git reset --hard",
    "uv run git reset --hard",
    "uv run rm -rf build",
    "uv run --no-sync git reset --hard",
    "caffeinate -i uv run git reset --hard",
    # 7. time goes before a case, and a heredoc body's substitution mustn't hide one
    'echo "$(time case x in a) git reset --hard;; esac)"',
    "echo $(time case x in a) git reset --hard;; esac)",
    'echo "$(time -p case x in a) git reset --hard;; esac)"',
    'echo "$(cat <<EOF\n$(date)\nEOF\ncase x in a) git reset --hard;; esac)"',
    # 8. eval by that exact word still runs
    "command eval 'git reset --hard'",
    # 10. pinned: each survived a mutation of the code
    'echo "\\\\$(git reset --hard)"',  # an escaped backslash doesn't escape the $
    "$x sudo git reset --hard",
    "cat <<'A'\nhi\nA\ngit reset --hard\necho more",
    "bash -cO extglob 'git reset --hard'",
    "bash -c +x 'git reset --hard'",
]


@pytest.mark.parametrize("command", ROUND_1_FORBIDDEN)
def test_round_1_blocks(repo: Path, command: str) -> None:
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 2, result.stderr


ROUND_1_ALLOWED = [
    # 1. a comment with nothing to misread, and a ) in a comment outside a $(...)
    'echo "$(echo a # plain note\n)"',
    "echo $(echo a # plain note\n)",
    "echo hi # (note) it's fine",
    "(echo a # a ) here\n)",
    # 2. normal commands that happen to hold -r and -f
    '"$py" -m pytest -rf',
    '"$py" -m pytest -q -rfE tests/',
    '"$py" -m pip install -r reqs.txt -f links',
    '"$py" -u run.py -r 5 -f out.json',
    '"$root/.venv/bin/python" -m pytest -rf',
    '"$FFPROBE" -v error -show_format -of json a.mp4',
    '"$ffprobe" -show_format a.mp4',
    '"$FFMPEG" -hide_banner -i a.mp4 -frames:v 1 o.jpg',
    '"$FFMPEG" -y -i a.mp4 -r 30 -f mp4 b.mp4',
    '"$CC" -O2 -fPIC -rdynamic a.c',
    "$x -r build",
    "$x -f build",
    # 3. a value-taking git option's value isn't the subcommand
    'git --git-dir "$dir" log --all --oneline',
    'git --git-dir "$dir" fetch origin main',
    'git --git-dir "$dir" log -f',
    'git --work-tree "$w" diff origin main',
    "git --git-dir .git status",
    "git --git-dir=.git status",
    # arguments after a $(...) are arguments. main split the command there and read them as a command
    "echo $(date) rm -rf build",
    # 4. a path whose last part is literal is that program, not an unknown one
    '"$root/scripts/sync.sh" push origin main',
    "$HOME/bin/deploy push origin main",
    '"$root/deploy/x.sh" -r -f',
    "$(git rev-parse --show-toplevel)/scripts/sync.sh push origin main",
    "${HOME}/bin/deploy push origin main",
    # 5. << after a closed (( )) opens a heredoc, and inside one it's a shift
    "(( x > 1 )) && cat <<'EOF'\ngit reset --hard\nEOF",
    "((i++)); cat <<'EOF'\nit's text\nEOF",
    "for ((i=0;i<3;i++)); do cat <<'EOF'\nrm -rf x\nEOF\ndone",
    "((x))\ncat <<'EOF'\nrm -rf x\nEOF",
    "((x<<2)); cat <<'EOF'\nit's text\nEOF",
    "((a)+(b<<2))\necho next",
    # 6. wrappers around allowed commands
    "/usr/bin/env python3 --version",
    "/usr/bin/time ls",
    "caffeinate -i uv run pytest -q",
    "timeout 5 git status",
    "nice -n 5 ls",
    "uv run pytest -q",
    "uv run t2c eval --only drafts",
    "uv run --no-sync ruff check .",
    # 7. the word case that doesn't start a command
    'echo "$(echo case)"',
    'echo "$(time echo case)"',
    # 8. eval is that exact word, not a program or a subcommand called eval
    './eval --note "don\'t"',
    'bin/eval --note "don\'t"',
    '"$t2c" eval --note "don\'t"',
    "\"$t2c\" eval --note 'git reset --hard'",
    'uv run t2c eval --note "don\'t"',
    # 10. pinned
    "cat <<'A'\nhi\nA\necho next\necho more",
    "bash -cO extglob 'echo hi'",
    "bash -c +x 'echo hi'",
]


@pytest.mark.parametrize("command", ROUND_1_ALLOWED)
def test_round_1_allows(repo: Path, command: str) -> None:
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 0, result.stderr
    assert "permissionDecision" not in result.stdout


@pytest.mark.parametrize("command", ["git --git-dir .git commit -m hi", "git --work-tree . commit -m hi"])
def test_a_value_taking_git_option_doesnt_hide_a_commit_on_main(repo: Path, command: str) -> None:
    git_switch_main(repo)
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 2, result.stderr


@pytest.mark.parametrize("command", ["git --git-dir .git commit -m hi", "git --work-tree . commit -m hi"])
def test_a_value_taking_git_option_allows_a_commit_off_main(repo: Path, command: str) -> None:
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "command",
    ['"$root/.venv/bin/pip" install foo', "uv run pip install foo", "timeout 5 pip install foo"],
)
def test_asks_before_a_dependency_added_behind_a_path_or_wrapper(repo: Path, command: str) -> None:
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize(
    "wrap",
    [lambda c: f"bash -c {shlex.quote(c)}", lambda c: f"eval {shlex.quote(c)}", lambda c: f'echo "$({c})"'],
    ids=["bash -c", "eval", "substitution"],
)
@pytest.mark.parametrize(("wraps", "code"), [(5, 0), (6, 2)])
def test_the_nesting_limit_is_exact(repo: Path, wrap: Callable[[str], str], wraps: int, code: int) -> None:
    command = "echo hi"
    for _ in range(wraps):
        command = wrap(command)
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == code, result.stderr


def test_an_expansion_then_another_still_guards_the_answer_key(repo: Path) -> None:
    result = run_hook("block_forbidden.py", {"command": "$x $y touch evals/expected/a"}, repo)
    assert result.returncode == 2, result.stderr
    assert "Answer Key" in result.stderr


@pytest.mark.parametrize("inner", ["git reset --hard", "date"])
def test_backticks_nested_in_quoted_backticks_are_refused(repo: Path, inner: str) -> None:
    """Unescaped once, they're unquoted backticks, which the scanner refuses wherever they sit."""
    result = run_hook("block_forbidden.py", {"command": f'echo "`echo \\`{inner}\\``"'}, repo)
    assert result.returncode == 2, result.stderr
    assert "wasn't checked" in result.stderr


def test_a_hung_git_blocks_the_command(tmp_path: Path) -> None:
    """A hung git isn't "not on main": it means the branch is unknown, so the command wasn't checked (T24)."""
    fake_git = tmp_path / "git"
    fake_git.write_text("#!/bin/sh\nexec sleep 8\n")
    fake_git.chmod(0o755)
    result = subprocess.run(
        [hook_python(), str(HOOKS / "block_forbidden.py")],
        input=json.dumps({"tool_input": {"command": "git commit -m x"}, "cwd": str(tmp_path)}),
        capture_output=True,
        text=True,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
    )
    assert result.returncode == 2
    assert "wasn't checked" in result.stderr


def test_a_folder_the_command_has_yet_to_make_is_not_a_failure(tmp_path: Path) -> None:
    result = run_hook("block_forbidden.py", {"command": "mkdir new && cd new && git status"}, tmp_path)
    assert result.returncode == 0, result.stderr


def wired_hook_command() -> str:
    settings = json.loads((HOOKS.parent / "settings.json").read_text())
    bash = next(entry for entry in settings["hooks"]["PreToolUse"] if entry["matcher"] == "Bash")
    return next(hook["command"] for hook in bash["hooks"] if "block_forbidden.py" in hook["command"])


def test_the_wiring_tests_run_on_the_python_under_test() -> None:
    """They find `python3` beside hook_python(), which uv names python3.9: if that were another version, they'd lie."""
    env = {"PATH": f"{Path(hook_python()).parent}:/usr/bin:/bin"}
    found = subprocess.run(["/bin/sh", "-c", "python3 --version"], capture_output=True, text=True, env=env)
    expected = subprocess.run([hook_python(), "--version"], capture_output=True, text=True)
    assert found.stdout == expected.stdout


def test_the_wiring_blocks_when_the_hook_exits_with_anything_else(tmp_path: Path) -> None:
    """A syntax error or an older Python never reaches main(), so the settings must turn exit 1 into a block (T24)."""
    hooks = tmp_path / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "block_forbidden.py").write_text("import sys\nsys.exit(1)\n")
    python_dir = Path(hook_python()).parent
    env = {"PATH": f"{python_dir}:/usr/bin:/bin", "CLAUDE_PROJECT_DIR": str(tmp_path)}
    result = subprocess.run(
        ["/bin/sh", "-c", wired_hook_command()], input="{}", text=True, capture_output=True, env=env
    )
    assert result.returncode == 2


def test_the_wiring_blocks_when_there_is_no_python3(tmp_path: Path) -> None:
    env = {"PATH": str(tmp_path), "CLAUDE_PROJECT_DIR": str(tmp_path)}
    result = subprocess.run(
        ["/bin/sh", "-c", wired_hook_command()], input="{}", text=True, capture_output=True, env=env
    )
    assert result.returncode == 2


def test_the_wiring_lets_an_allowed_command_through(repo: Path) -> None:
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(HOOKS.parent.parent),
        "PATH": f"{Path(hook_python()).parent}:/usr/bin:/bin",
    }
    payload = json.dumps({"tool_input": {"command": "git status"}, "cwd": str(repo)})
    result = subprocess.run(
        ["/bin/sh", "-c", wired_hook_command()], input=payload, text=True, capture_output=True, env=env
    )
    assert result.returncode == 0, result.stderr


def test_ruff_judges_the_hooks_as_python_39() -> None:
    """Ruff's B905 asks for zip(strict=True), which is how 3.10-only code got into a hook (T24)."""
    source = "words = [1]\nraw = [2]\npairs = list(zip(words, raw))\n"
    project = HOOKS.parent.parent
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "B905",
            "--stdin-filename",
            str(HOOKS / "any_hook.py"),
            "-",
        ],
        input=source,
        capture_output=True,
        text=True,
        cwd=project,
    )
    assert result.returncode == 0, result.stdout


# --- prompt_changed.py --------------------------------------------------------


def test_reminds_to_run_eval_set_after_prompt_edit() -> None:
    result = run_hook("prompt_changed.py", {"file_path": "/repo/src/agents/checker/prompts/system.md"})
    assert result.returncode == 0
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Eval Set" in context


def test_stays_quiet_for_other_files() -> None:
    result = run_hook("prompt_changed.py", {"file_path": "/repo/src/agents/checker/agent.py"})
    assert result.returncode == 0
    assert result.stdout == ""


# --- format_on_edit.py --------------------------------------------------------


def test_format_hook_never_blocks_on_non_python_files() -> None:
    result = run_hook("format_on_edit.py", {"file_path": "/repo/README.md"})
    assert result.returncode == 0


# --- settings.json ------------------------------------------------------------


def test_settings_wire_every_hook_file() -> None:
    settings = (HOOKS.parent / "settings.json").read_text()
    for hook in HOOKS.glob("*.py"):
        assert hook.name in settings, f"{hook.name} exists but is not wired in settings.json"


# --- .githooks/pre-commit (T14): an Answer Key change needs a person to say so -------

GITHOOKS = HOOKS.parent.parent / ".githooks"


def commit(repo: Path, path: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    import os

    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", path], check=True)
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "x"],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


@pytest.fixture
def hooked(repo: Path) -> Path:
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", str(GITHOOKS)], check=True)
    return repo


def test_pre_commit_refuses_an_answer_key_change(hooked: Path) -> None:
    result = commit(hooked, "evals/expected/charge.toml")
    assert result.returncode != 0
    assert "Answer Key" in result.stderr and "T2C_ANSWER_KEY_EDIT=1" in result.stderr


def test_pre_commit_lets_a_person_change_the_answer_key_on_purpose(hooked: Path) -> None:
    assert commit(hooked, "evals/expected/charge.toml", {"T2C_ANSWER_KEY_EDIT": "1"}).returncode == 0


def test_pre_commit_lets_everything_else_through(hooked: Path) -> None:
    assert commit(hooked, "evals/films/charge/film.toml").returncode == 0


@pytest.mark.parametrize(
    "command",
    [
        "T2C_ANSWER_KEY_EDIT=1 git commit -m x",
        "export T2C_ANSWER_KEY_EDIT=1 && git commit -m x",
        "env T2C_ANSWER_KEY_EDIT=1 git commit -m x",
    ],
)
def test_the_answer_key_override_is_for_people_only(repo: Path, command: str) -> None:
    result = run_hook("block_forbidden.py", {"command": command}, repo)
    assert result.returncode == 2 and "T2C_ANSWER_KEY_EDIT" in result.stderr
