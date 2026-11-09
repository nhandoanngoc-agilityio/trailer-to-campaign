"""Tests for the /preflight script in .claude/skills/preflight/.

Each test runs the script as a subprocess on the machine's python3, as /preflight does, in a throwaway git repo.
`gh`, `uv` and `ffmpeg` are fakes, and PATH holds only what the test allows, so nothing depends on this machine.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
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
        self.git("config", "--unset-all", "remote.origin.pushurl") if self.has_push_url() else None
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


def test_gh_asks_about_the_repo_it_would_really_act_on(project: Project):
    """Naming the repo would hide a GH_REPO that points somewhere else."""
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


def test_outside_a_git_repo_fails_with_a_plain_message(project: Project, tmp_path: Path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    done = subprocess.run(
        [hook_python(), str(SCRIPT)], cwd=elsewhere, env=project.child_env(), capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 1
    assert "FAIL" in done.stdout
    assert "Traceback" not in done.stderr
