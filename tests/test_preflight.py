"""Tests for the /preflight script in .claude/skills/preflight/.

Each test runs the script as a subprocess on the machine's python3, as /preflight does, in a throwaway git repo.
`gh`, `uv` and `ffmpeg` are fakes, and PATH holds only what the test allows, so nothing depends on this machine.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from test_hooks import hook_python

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".claude" / "skills" / "preflight" / "preflight.py"
SLUG = "nhandoanngoc-agilityio/trailer-to-campaign"
GOOD_URL = f"https://github.com/{SLUG}.git"
GOOD_VIEW = json.dumps({"nameWithOwner": SLUG, "url": f"https://github.com/{SLUG}"})
# Slept for in a child of the fake, not exec'd, so the child holds the pipe after the fake itself is killed.
HANG = "/bin/sleep 60"


@dataclass
class Result:
    code: int
    out: str
    err: str
    seconds: float

    def row(self, name: str) -> str:
        """The status printed for one check. A missing row fails the test."""
        for line in self.out.splitlines():
            if line[8:23].strip() == name:
                return line[:8].strip()
        raise AssertionError(f"no row for {name!r} in:\n{self.out}")

    def line(self, name: str) -> str:
        for line in self.out.splitlines():
            if line[8:23].strip() == name:
                return line
        raise AssertionError(f"no row for {name!r} in:\n{self.out}")


class Project:
    """A throwaway copy of the parts of this repo that /preflight looks at, with everything healthy."""

    def __init__(self, root: Path, bin_dir: Path, log: Path, home: Path) -> None:
        self.root = root
        self.bin_dir = bin_dir
        self.log = log
        self.home = home
        self.env_extra: dict[str, str] = {}

    def fake(self, name: str, body: str) -> None:
        path = self.bin_dir / name
        path.write_text(f'#!/bin/sh\nprintf "%s\\n" "{name} $*" >> "{self.log}"\n{body}\n')
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def fake_gh(self, view: str = "", view_exit: int = 0, api: str = "", api_exit: int = 0, hang: bool = False) -> None:
        if hang:
            self.fake("gh", HANG)
            return
        self.fake(
            "gh",
            f"""case "$1" in
  repo) /bin/cat <<'EOF'
{view}
EOF
    [ {view_exit} -eq 0 ] || echo "HTTP 403: denied" >&2
    exit {view_exit} ;;
  api) /bin/cat <<'EOF'
{api}
EOF
    [ {api_exit} -eq 0 ] || echo "HTTP 422: No commit found" >&2
    exit {api_exit} ;;
esac
exit 64""",
        )

    def remove_tool(self, name: str) -> None:
        (self.bin_dir / name).unlink()

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=self.root,
            env=self.child_env(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert done.returncode == 0, done.stderr
        return done.stdout

    def child_env(self) -> dict[str, str]:
        return {
            "PATH": str(self.bin_dir),
            "HOME": str(self.home),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            **self.env_extra,
        }

    def set_origin(self, url: str, push: str | None = None) -> None:
        self.git("remote", "set-url", "origin", url)
        if self.has_push_url():
            self.git("config", "--unset-all", "remote.origin.pushurl")
        if push:
            self.git("remote", "set-url", "--push", "origin", push)

    def has_push_url(self) -> bool:
        return "pushurl" in self.git("config", "--get-regexp", "^remote\\.origin\\.")

    def calls(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def run(self, *args: str, timeout: str = "20") -> Result:
        started = time.monotonic()
        done = subprocess.run(
            [hook_python(), str(SCRIPT), "--timeout", timeout, *args],
            cwd=self.root,
            env=self.child_env(),
            capture_output=True,
            text=True,
            timeout=30,  # so a hang fails the test instead of holding CI
        )
        return Result(done.returncode, done.stdout, done.stderr, time.monotonic() - started)


@pytest.fixture
def project(tmp_path: Path) -> Project:
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    shutil.copytree(
        ROOT / ".claude" / "hooks", root / ".claude" / "hooks", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copytree(
        ROOT / ".claude" / "skills", root / ".claude" / "skills", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copy(ROOT / ".claude" / "settings.json", root / ".claude" / "settings.json")
    shutil.copytree(ROOT / ".githooks", root / ".githooks")
    (root / ".github" / "workflows").mkdir(parents=True)
    shutil.copy(ROOT / ".github" / "workflows" / "ci.yml", root / ".github" / "workflows" / "ci.yml")
    (root / ".gitignore").write_text(".venv/\n.env\n")
    venv_python = root / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text('#!/bin/sh\necho "Python 3.11.9"\n')
    venv_python.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = shutil.which("git")
    assert git, "these tests need git"
    (bin_dir / "git").symlink_to(git)
    home = tmp_path / "home"
    home.mkdir()

    made = Project(root, bin_dir, tmp_path / "calls.log", home)
    made.git("init", "-q", "-b", "t1-skeleton")
    made.git("remote", "add", "origin", GOOD_URL)
    made.git("config", "core.hooksPath", ".githooks")
    made.git("add", "-A")
    made.git("commit", "-q", "-m", "start")
    made.fake_gh(view=GOOD_VIEW)
    made.fake("uv", 'echo "Resolved 61 packages in 3ms"')
    made.fake("ffmpeg", 'echo "ffmpeg version 8"')
    return made


# --- remote -------------------------------------------------------------------


def test_a_gitlab_origin_fails_even_when_gh_reports_success(project: Project):
    """The false pass this ticket exists for: `gh` looks fine, and it's asking the wrong place."""
    project.set_origin(f"https://gitlab.com/{SLUG}.git")
    result = project.run()
    assert result.row("github access") == "PASS"  # gh really did report success
    assert result.row("remote") == "FAIL"
    assert result.code == 1


@pytest.mark.parametrize(
    "url",
    [
        f"https://github.com.evil.com/{SLUG}.git",
        f"https://github.com@evil.com/{SLUG}.git",
        f"https://gitlab.com/github.com/{SLUG}.git",
        f"git@gitlab.com:{SLUG}.git",
        f"git@gitlab.com:github.com/{SLUG}.git",
        f"http://github.com/{SLUG}.git",
        "https://github.com/someone-else/trailer-to-campaign.git",
        f"https://github.com/{SLUG}-fork.git",
        "/tmp/local/copy.git",
    ],
)
def test_a_remote_that_is_not_this_repo_on_github_fails(project: Project, url: str):
    project.set_origin(url)
    assert project.run().row("remote") == "FAIL"


def test_a_push_url_on_another_host_fails(project: Project):
    project.set_origin(GOOD_URL, push=f"https://gitlab.com/{SLUG}.git")
    assert project.run().row("remote") == "FAIL"


def test_no_origin_fails(project: Project):
    project.git("remote", "remove", "origin")
    assert project.run().row("remote") == "FAIL"


@pytest.mark.parametrize(
    "url",
    [
        f"https://github.com/{SLUG}.git",
        f"https://github.com/{SLUG}",
        f"https://github.com/{SLUG}/",
        f"git@github.com:{SLUG}.git",
        f"ssh://git@github.com/{SLUG}.git",
        f"https://GitHub.com/{SLUG.upper()}.git",
    ],
)
def test_this_repo_on_github_passes_in_every_form(project: Project, url: str):
    project.set_origin(url)
    assert project.run().row("remote") == "PASS"


def test_a_token_in_the_remote_never_reaches_the_output(project: Project):
    project.set_origin(f"https://someone:ghp_sekret123@gitlab.com/{SLUG}.git")
    result = project.run()
    assert result.row("remote") == "FAIL"
    assert "sekret" not in result.out + result.err


# --- github access ------------------------------------------------------------


def test_gh_returning_a_different_repo_fails(project: Project):
    project.fake_gh(view=json.dumps({"nameWithOwner": "other/repo", "url": "https://github.com/other/repo"}))
    result = project.run()
    assert result.row("github access") == "FAIL"
    assert result.code == 1


def test_gh_on_another_host_fails(project: Project):
    project.fake_gh(view=json.dumps({"nameWithOwner": SLUG, "url": f"https://ghe.example.com/{SLUG}"}))
    assert project.run().row("github access") == "FAIL"


@pytest.mark.parametrize(
    "value", ["cli/cli", "github.com/cli/cli", f"gitlab.com/{SLUG}", "https://github.com/cli/cli", "x"]
)
def test_a_gh_repo_that_points_elsewhere_fails(project: Project, value: str):
    """Tried on real gh: `gh repo view` ignores GH_REPO and reports this repo, while `gh issue` and `gh pr` obey it."""
    project.env_extra["GH_REPO"] = value
    result = project.run()
    assert result.row("github access") == "FAIL"
    assert "GH_REPO" in result.line("github access")
    assert result.code == 1


@pytest.mark.parametrize("value", [SLUG, SLUG.upper(), f"github.com/{SLUG}", f"https://github.com/{SLUG}.git", ""])
def test_a_gh_repo_that_points_here_passes(project: Project, value: str):
    project.env_extra["GH_REPO"] = value
    assert project.run().row("github access") == "PASS"


def test_gh_asks_about_the_repo_this_folder_resolves_to(project: Project):
    """Naming the repo would pass whatever this folder's remotes say."""
    project.run()
    view = next(line for line in project.calls().splitlines() if line.startswith("gh repo view"))
    assert SLUG not in view


def test_gh_denied_fails_and_shows_why(project: Project):
    project.fake_gh(view_exit=1)
    result = project.run()
    assert result.row("github access") == "FAIL"
    assert "403" in result.line("github access")


def test_gh_missing_fails(project: Project):
    project.remove_tool("gh")
    assert project.run().row("github access") == "FAIL"


def test_gh_returning_this_repo_passes(project: Project):
    assert project.run().row("github access") == "PASS"


def test_gh_output_that_cannot_be_read_is_unknown_never_a_pass(project: Project):
    project.fake_gh(view="<html>a proxy said hello</html>")
    assert project.run().row("github access") == "UNKNOWN"


def test_gh_output_with_missing_fields_is_unknown(project: Project):
    project.fake_gh(view="{}")
    assert project.run().row("github access") == "UNKNOWN"


# --- timeouts -----------------------------------------------------------------


def test_a_hung_gh_is_unknown_and_does_not_hold_the_run(project: Project):
    project.fake_gh(hang=True)
    result = project.run(timeout="1")
    assert result.row("github access") == "UNKNOWN"
    assert result.code != 0
    assert result.seconds < 15


def test_a_timeout_survives_macos_refusing_to_signal_the_group(monkeypatch: pytest.MonkeyPatch):
    """macOS can answer PermissionError from killpg for a group that's ending. That must stay a timeout, not a crash."""

    def refuse(*_: object) -> None:
        raise PermissionError

    module = load_script()
    monkeypatch.setattr(module.os, "killpg", refuse)
    started = time.monotonic()
    assert module.run(["/bin/sleep", "60"], 0.2).outcome == "timeout"
    assert time.monotonic() - started < 15


def test_outside_a_git_repo_fails_with_a_plain_message(project: Project, tmp_path: Path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    done = subprocess.run(
        [hook_python(), str(SCRIPT)], cwd=elsewhere, env=project.child_env(), capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 1
    assert "FAIL" in done.stdout
    assert "Traceback" not in done.stderr


# --- git hook -----------------------------------------------------------------


def test_the_git_hook_set_up_passes(project: Project):
    assert project.run().row("git hook") == "PASS"


def test_a_git_hook_path_that_is_not_set_fails(project: Project):
    project.git("config", "--unset", "core.hooksPath")
    result = project.run()
    assert result.row("git hook") == "FAIL"
    assert "git config core.hooksPath .githooks" in result.out


def test_a_git_hook_path_elsewhere_fails(project: Project):
    project.git("config", "core.hooksPath", "/tmp/elsewhere")
    assert project.run().row("git hook") == "FAIL"


def test_a_pre_commit_that_cannot_run_fails(project: Project):
    (project.root / ".githooks" / "pre-commit").chmod(0o644)
    assert project.run().row("git hook") == "FAIL"


# --- claude hooks -------------------------------------------------------------

ALLOWS_ALL = "import sys; sys.exit(0)\n"
BLOCKS_ALL = "import sys; print('Blocked by team policy: everything', file=sys.stderr); sys.exit(2)\n"
ASKS_ON_ALL = """import json
print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask"}}))
"""
CRASHES = "import sys; print('Blocked: the guardrail hook failed (boom)', file=sys.stderr); sys.exit(2)\n"
HANGS = "import time; time.sleep(60)\n"


def settings(project: Project) -> dict:
    return json.loads((project.root / ".claude" / "settings.json").read_text())


def write_settings(project: Project, data: dict) -> None:
    (project.root / ".claude" / "settings.json").write_text(json.dumps(data))


def test_the_real_hook_passes(project: Project):
    assert project.run().row("claude hooks") == "PASS"


@pytest.mark.parametrize(
    "hook", [ALLOWS_ALL, BLOCKS_ALL, ASKS_ON_ALL, CRASHES], ids=["allows", "blocks", "asks", "crash"]
)
def test_a_hook_that_does_not_tell_the_two_commands_apart_fails(project: Project, hook: str):
    (project.root / ".claude" / "hooks" / "block_forbidden.py").write_text(hook)
    result = project.run()
    assert result.row("claude hooks") == "FAIL"
    assert result.code == 1


def test_a_hung_hook_is_unknown(project: Project):
    (project.root / ".claude" / "hooks" / "block_forbidden.py").write_text(HANGS)
    result = project.run(timeout="1")
    assert result.row("claude hooks") == "UNKNOWN"
    assert result.seconds < 15


def test_the_commands_are_only_sent_never_executed(project: Project):
    project.git("commit", "-q", "--allow-empty", "-m", "second")
    head = project.git("rev-parse", "HEAD")
    marker = project.root / "untracked-marker.txt"
    marker.write_text("still here")
    project.run()
    assert project.git("rev-parse", "HEAD") == head
    assert marker.read_text() == "still here"


def test_a_missing_hook_script_fails(project: Project):
    (project.root / ".claude" / "hooks" / "format_on_edit.py").unlink()
    assert project.run().row("claude hooks") == "FAIL"


def test_a_hook_that_is_not_wired_fails_even_when_the_script_works(project: Project):
    data = settings(project)
    del data["hooks"]["PreToolUse"]
    write_settings(project, data)
    assert project.run().row("claude hooks") == "FAIL"


def test_the_hook_wired_to_another_tool_fails(project: Project):
    data = settings(project)
    data["hooks"]["PreToolUse"][0]["matcher"] = "Edit"
    write_settings(project, data)
    assert project.run().row("claude hooks") == "FAIL"


def test_bash_in_a_matcher_list_passes(project: Project):
    data = settings(project)
    data["hooks"]["PreToolUse"][0]["matcher"] = "Edit|Bash"
    write_settings(project, data)
    assert project.run().row("claude hooks") == "PASS"


@pytest.mark.parametrize("file", ["settings.json", "settings.local.json"])
def test_hooks_turned_off_fails(project: Project, file: str):
    path = project.root / ".claude" / file
    data = json.loads(path.read_text()) if path.exists() else {}
    path.write_text(json.dumps({**data, "disableAllHooks": True}))
    assert project.run().row("claude hooks") == "FAIL"


@pytest.mark.parametrize("file", ["settings.json", "settings.local.json"])
def test_settings_that_cannot_be_read_fail(project: Project, file: str):
    (project.root / ".claude" / file).write_text("{not json")
    assert project.run().row("claude hooks") == "FAIL"


def test_no_local_settings_passes(project: Project):
    assert not (project.root / ".claude" / "settings.local.json").exists()
    assert project.run().row("claude hooks") == "PASS"


# --- toolchain ----------------------------------------------------------------


def set_venv_python(project: Project, version: str) -> None:
    (project.root / ".venv" / "bin" / "python").write_text(f'#!/bin/sh\necho "Python {version}"\n')


def test_the_same_python_as_ci_a_current_lock_and_ffmpeg_pass(project: Project):
    assert project.run().row("toolchain") == "PASS"


def test_another_python_than_ci_fails_and_names_both(project: Project):
    set_venv_python(project, "3.14.2")
    result = project.run()
    assert result.row("toolchain") == "FAIL"
    assert "3.14" in result.line("toolchain")
    assert "3.11" in result.line("toolchain")
    assert "uv sync --python 3.11" in result.out


def test_a_python_that_only_starts_the_same_is_a_mismatch(project: Project):
    set_venv_python(project, "3.1.9")
    assert project.run().row("toolchain") == "FAIL"


def test_no_venv_fails(project: Project):
    (project.root / ".venv" / "bin" / "python").unlink()
    assert project.run().row("toolchain") == "FAIL"


def test_a_stale_lock_fails(project: Project):
    project.fake("uv", 'echo "hint: To update the lockfile, run uv lock." >&2; exit 1')
    result = project.run()
    assert result.row("toolchain") == "FAIL"
    assert "uv lock" in result.out


def test_a_lock_that_could_not_be_checked_is_unknown_not_stale(project: Project):
    """`uv lock --check` also exits 1 when the network is down, which says nothing about the lock."""
    project.fake("uv", 'echo "Packages were unavailable because the network was disabled." >&2; exit 1')
    assert project.run().row("toolchain") == "UNKNOWN"


def test_uv_missing_fails(project: Project):
    project.remove_tool("uv")
    assert project.run().row("toolchain") == "FAIL"


def test_ffmpeg_missing_fails(project: Project):
    project.remove_tool("ffmpeg")
    assert project.run().row("toolchain") == "FAIL"


def test_a_broken_ffmpeg_fails(project: Project):
    project.fake("ffmpeg", "exit 1")
    assert project.run().row("toolchain") == "FAIL"


def test_a_hung_uv_is_unknown(project: Project):
    project.fake("uv", HANG)
    result = project.run(timeout="1")
    assert result.row("toolchain") == "UNKNOWN"
    assert result.seconds < 15


def test_a_fail_outranks_an_unknown_in_the_same_row(project: Project):
    set_venv_python(project, "3.14.2")
    project.fake("uv", HANG)
    assert project.run(timeout="1").row("toolchain") == "FAIL"


@pytest.mark.parametrize(
    "text",
    [
        "jobs: {}\n",
        '          python-version: "3.11"\n          python-version: "3.12"\n',
        "          python-version: ${{ matrix.python }}\n",
        '      # python-version: "3.11"\n',
    ],
    ids=["none", "two", "matrix", "comment"],
)
def test_a_ci_file_without_exactly_one_clear_python_is_unknown(project: Project, text: str):
    (project.root / ".github" / "workflows" / "ci.yml").write_text(text)
    assert project.run().row("toolchain") == "UNKNOWN"


def test_a_check_that_crashes_is_unknown_and_the_others_still_run(project: Project):
    (project.root / ".github" / "workflows" / "ci.yml").write_bytes(b"\xff\xfe not text")
    result = project.run()
    assert result.row("toolchain") == "UNKNOWN"
    assert result.row("skill files") == "PASS"
    assert "Traceback" not in result.err


# --- skill files --------------------------------------------------------------


def write_skill(project: Project, name: str, text: str) -> None:
    folder = project.root / ".claude" / "skills" / name
    folder.mkdir(exist_ok=True)
    (folder / "SKILL.md").write_text(text)


def test_the_real_skills_pass(project: Project):
    assert project.run().row("skill files") == "PASS"


def test_angle_bracket_words_like_ship_tickets_are_not_placeholders(project: Project):
    write_skill(project, "fine", "---\nname: fine\ndescription: d\n---\nRun `gh pr view <pr>` for T<t>.\n")
    assert project.run().row("skill files") == "PASS"


@pytest.mark.parametrize(
    "text",
    [
        "no frontmatter at all\n",
        "---\nname: bad\ndescription: never closed\n",
        "---\ndescription: no name\n---\nBody\n",
        "---\nname: bad\n---\nNo description\n",
        "---\nname: bad\ndescription:\n---\nEmpty description\n",
        "---\nname: another-name\ndescription: d\n---\nThe wrong name\n",
        "---\nname: bad\ndescription: d\n---\nFill in {{ project }}\n",
        "---\nname: bad\ndescription: d\n---\nSet <your name> here\n",
        "---\nname: bad\ndescription: d\n---\nTODO write this\n",
        "---\nname: bad\ndescription: d\n---\nPLACEHOLDER\n",
        "---\nname: bad\ndescription: d\n---\nREPLACE_ME\n",
    ],
)
def test_a_bad_skill_file_fails_and_is_named(project: Project, text: str):
    write_skill(project, "bad", text)
    result = project.run()
    assert result.row("skill files") == "FAIL"
    assert "bad" in result.line("skill files")


def test_a_skill_folder_without_a_skill_file_fails(project: Project):
    (project.root / ".claude" / "skills" / "empty").mkdir()
    assert project.run().row("skill files") == "FAIL"


def test_no_skills_at_all_fails(project: Project):
    shutil.rmtree(project.root / ".claude" / "skills")
    assert project.run().row("skill files") == "FAIL"


# --- ci -----------------------------------------------------------------------


def check_runs(*runs: tuple[str, str, str | None], total: int | None = None) -> str:
    made = [{"name": name, "status": status, "conclusion": conclusion} for name, status, conclusion in runs]
    return json.dumps({"total_count": len(made) if total is None else total, "check_runs": made})


# What `main` really reported on 2026-11-09: a push doesn't run the PR-only checks.
MAIN_SHAPE = check_runs(
    ("claude", "completed", "skipped"), ("answer-key", "completed", "skipped"), ("test", "completed", "success")
)


def test_the_real_shape_from_main_passes(project: Project):
    project.fake_gh(view=GOOD_VIEW, api=MAIN_SHAPE)
    result = project.run("--ci")
    assert result.row("ci") == "PASS"
    assert result.code == 0


def test_ci_asks_github_about_this_commit(project: Project):
    project.fake_gh(view=GOOD_VIEW, api=MAIN_SHAPE)
    project.run("--ci")
    api = next(line for line in project.calls().splitlines() if line.startswith("gh api"))
    assert f"repos/{SLUG}/commits/{project.git('rev-parse', 'HEAD').strip()}/check-runs" in api
    assert "--hostname github.com" in api


@pytest.mark.parametrize(
    "api",
    [
        check_runs(),
        check_runs(("test", "in_progress", None)),
        check_runs(("test", "queued", None)),
        check_runs(("test", "completed", "success"), ("answer-key", "in_progress", None)),
        check_runs(("test", "completed", "skipped")),
        check_runs(("answer-key", "completed", "success")),
        check_runs(("claude", "completed", "skipped")),
        check_runs(("test", "completed", "success"), total=101),
        "not json",
    ],
    ids=[
        "none",
        "running",
        "queued",
        "another-pending",
        "test-skipped",
        "no-test",
        "all-skipped",
        "too-many",
        "bad-json",
    ],
)
def test_ci_it_cannot_confirm_is_unknown(project: Project, api: str):
    project.fake_gh(view=GOOD_VIEW, api=api)
    result = project.run("--ci")
    assert result.row("ci") == "UNKNOWN"
    assert result.code == 2


@pytest.mark.parametrize(
    "conclusion", ["failure", "timed_out", "cancelled", "stale", "action_required", "startup_failure"]
)
@pytest.mark.parametrize("name", ["test", "answer-key"])
def test_a_bad_result_on_any_check_fails(project: Project, name: str, conclusion: str):
    runs = [("test", "completed", "success"), (name, "completed", conclusion)]
    project.fake_gh(view=GOOD_VIEW, api=check_runs(*runs))
    result = project.run("--ci")
    assert result.row("ci") == "FAIL"
    assert name in result.line("ci")
    assert result.code == 1


def test_a_failed_check_outranks_one_still_running(project: Project):
    runs = [("test", "in_progress", None), ("answer-key", "completed", "failure")]
    project.fake_gh(view=GOOD_VIEW, api=check_runs(*runs))
    assert project.run("--ci").row("ci") == "FAIL"


def test_a_commit_github_does_not_have_is_unknown(project: Project):
    project.fake_gh(view=GOOD_VIEW, api_exit=1)
    result = project.run("--ci")
    assert result.row("ci") == "UNKNOWN"
    assert result.code == 2


def test_a_hung_ci_call_is_unknown(project: Project):
    project.fake(
        "gh",
        f"""if [ "$1" = api ]; then {HANG}; fi
/bin/cat <<'EOF'
{GOOD_VIEW}
EOF""",
    )
    result = project.run("--ci", timeout="1")
    assert result.row("ci") == "UNKNOWN"
    assert result.seconds < 15


def test_ci_does_not_ask_when_origin_is_the_wrong_place(project: Project):
    """The false pass again: a green answer about another repo says nothing about this commit."""
    project.fake_gh(view=GOOD_VIEW, api=MAIN_SHAPE)
    project.set_origin(f"https://gitlab.com/{SLUG}.git")
    result = project.run("--ci")
    assert result.row("ci") == "UNKNOWN"
    assert result.code == 1  # the remote's own FAIL
    assert "gh api" not in project.calls()


def test_without_ci_the_check_is_left_out_and_the_report_says_so(project: Project):
    project.fake_gh(view=GOOD_VIEW, api=check_runs(("test", "completed", "failure")))
    result = project.run()
    assert result.row("ci") == "SKIP"
    assert "--ci" in result.line("ci")
    assert result.code == 0  # SKIP is left out of the exit code, and the report says so
    assert "gh api" not in project.calls()


def test_ci_says_it_only_covers_the_commit_when_files_have_changed(project: Project):
    project.fake_gh(view=GOOD_VIEW, api=MAIN_SHAPE)
    (project.root / "changed.txt").write_text("not committed")
    result = project.run("--ci")
    assert result.row("ci") == "PASS"
    assert "uncommitted" in result.line("ci")


# --- the report as a whole ----------------------------------------------------


def test_all_healthy_exits_0_with_a_row_for_every_check(project: Project):
    result = project.run()
    assert result.code == 0, result.out
    for name in ("remote", "github access", "git hook", "claude hooks", "toolchain", "skill files"):
        assert result.row(name) == "PASS"


def test_a_fail_exits_1(project: Project):
    project.git("config", "--unset", "core.hooksPath")
    assert project.run().code == 1


def test_an_unknown_with_no_fail_exits_2(project: Project):
    project.fake_gh(view="not json")
    assert project.run().code == 2


def test_a_fail_outranks_an_unknown_in_the_exit_code(project: Project):
    project.fake_gh(view="not json")
    project.git("config", "--unset", "core.hooksPath")
    assert project.run().code == 1


def test_every_row_that_is_not_a_pass_prints_a_fix(project: Project):
    project.git("config", "--unset", "core.hooksPath")
    project.remove_tool("uv")
    project.fake_gh(view="not json")
    project.set_origin(f"https://gitlab.com/{SLUG}.git")
    lines = project.run().out.splitlines()
    bad = [i for i, line in enumerate(lines) if line.startswith(("FAIL", "UNKNOWN"))]
    assert len(bad) == 4
    for i in bad:
        assert lines[i + 1].strip().startswith("fix: ")


def test_a_control_character_from_a_tool_cannot_forge_a_row(project: Project):
    project.fake_gh(view="nope\nPASS    remote         all good\x1b[2J")
    result = project.run()
    assert result.row("github access") == "UNKNOWN"
    assert "\x1b" not in result.out
    assert sum(line.startswith("PASS    remote") for line in result.out.splitlines()) == 1


# --- changes nothing ----------------------------------------------------------


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def test_a_full_run_changes_nothing(project: Project):
    project.fake_gh(view=GOOD_VIEW, api=MAIN_SHAPE)
    before = snapshot(project.root)
    status = project.git("status", "--porcelain", "--ignored")
    config = (project.root / ".git" / "config").read_bytes()
    project.run("--ci")
    assert snapshot(project.root) == before
    assert project.git("status", "--porcelain", "--ignored") == status
    assert (project.root / ".git" / "config").read_bytes() == config


def test_a_failing_run_changes_nothing_either(project: Project):
    """The fixes are printed, never applied."""
    project.git("config", "--unset", "core.hooksPath")
    set_venv_python(project, "3.14.2")
    before = snapshot(project.root)
    config = (project.root / ".git" / "config").read_bytes()
    project.run()
    assert snapshot(project.root) == before
    assert (project.root / ".git" / "config").read_bytes() == config
    assert "sync" not in project.calls()


def test_a_dot_env_is_never_read(project: Project):
    (project.root / ".env").write_text("SLACK_BOT_TOKEN=sentinel-value-for-the-test\n")
    project.fake_gh(view=GOOD_VIEW, api=MAIN_SHAPE)
    result = project.run("--ci")
    assert "sentinel-value" not in result.out + result.err
    assert not re.search(r"\.env\b", SCRIPT.read_text())


# --- no drift -----------------------------------------------------------------


def load_script():
    spec = importlib.util.spec_from_file_location("preflight", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["preflight"] = module  # NamedTuple looks its module up here while the file loads
    spec.loader.exec_module(module)
    return module


def test_the_python_read_from_ci_yml_is_what_yaml_says():
    """The script can't use PyYAML on the machine's python3, so this is what stops its line match drifting."""
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    found = [
        step["with"]["python-version"]
        for job in yaml.safe_load(text)["jobs"].values()
        for step in job["steps"]
        if "python-version" in step.get("with", {})
    ]
    assert found == [load_script().ci_python(text)]


def test_ci_runs_these_tests_on_python_3_9():
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]
    step = next(step for step in jobs["test"]["steps"] if step.get("name") == "Test the hooks on Python 3.9")
    assert "tests/test_preflight.py" in step["run"]


def test_the_script_is_python_3_9_and_standard_library_only():
    tree = ast.parse(SCRIPT.read_text(), feature_version=(3, 9))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= set(sys.stdlib_module_names)


def test_the_checks_name_the_commands_the_skill_says_it_runs():
    skill = (SCRIPT.parent / "SKILL.md").read_text()
    assert "python3 .claude/skills/preflight/preflight.py" in skill
    assert "disable-model-invocation: true" in skill


def test_the_skill_never_puts_its_arguments_in_a_command():
    skill = (SCRIPT.parent / "SKILL.md").read_text()
    commands = re.findall(r"```.*?```", skill, re.DOTALL)
    assert commands
    assert not any("$ARGUMENTS" in command or "$1" in command for command in commands)
    assert "`$ARGUMENTS`" not in skill


def test_the_skill_has_the_session_tools_step_and_keeps_it_out_of_the_exit_code():
    skill = (SCRIPT.parent / "SKILL.md").read_text()
    assert "## 2. Add the session tools" in skill
    assert "isn't part of the script's exit code" in skill
    assert "Don't repair anything" in skill
