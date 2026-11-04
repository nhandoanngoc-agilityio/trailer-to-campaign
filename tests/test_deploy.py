"""Tests for the deploy kit (T26): deploy/t2c-service and deploy/run-slack.sh, with launchctl, uv and t2c faked."""

from __future__ import annotations

import os
import plistlib
import stat
import subprocess
import time
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
SERVICE = DEPLOY / "t2c-service"
RUN_SLACK = DEPLOY / "run-slack.sh"
SECRET_VALUES = {
    "ANTHROPIC_API_KEY": "sk-ant-secret-1",
    "SLACK_BOT_TOKEN": "xoxb-secret-2",
    "SLACK_APP_TOKEN": "xapp-secret-3",
    "T2C_MARKETERS": "U0SECRET4",
}
LABEL = "io.t2c.slack"


def fake_tool(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


class Kit:
    """A throwaway repo with one tag, a service home and fake system tools."""

    def __init__(self, tmp: Path) -> None:
        self.home = tmp / "home"
        self.t2c_home = self.home / ".t2c"
        self.repo = tmp / "repo"
        self.bin = tmp / "bin"
        self.calls = tmp / "calls.log"
        self.home.mkdir()
        self.repo.mkdir()
        self.bin.mkdir()
        (self.home / "Library" / "LaunchAgents").mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        (self.repo / "deploy").mkdir()
        (self.repo / "deploy" / "run-slack.sh").write_text("#!/bin/sh\n")
        (self.repo / "pyproject.toml").write_text("[project]\nname='x'\nversion='1'\n")
        film = self.repo / "films" / "demo-film"
        (film / "source").mkdir(parents=True)
        (film / "film.toml").write_text("title='x'\n")
        (film / "source" / "trailer.webm").write_text("footage")
        (self.repo / ".gitignore").write_text("films/\n")
        self.git("add", ".")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one")
        self.git("tag", "v0.1.0")
        self.loaded = tmp / "loaded"
        fake_tool(self.bin, "plutil", f'echo "plutil $*" >> "{self.calls}"\nexit 0')
        # uv's sync makes the release's entry point, as the real one does.
        fake_tool(
            self.bin,
            "uv",
            f'''echo "uv $*" >> "{self.calls}"
for last; do :; done
mkdir -p "$last/.venv/bin" && : > "$last/.venv/bin/t2c"''',
        )
        self.fake_launchctl()

    def fake_launchctl(self, stuck: bool = False, never_runs: bool = False) -> None:
        """Like launchd, it knows only a loaded service: print fails once it's gone."""
        unload = ":" if stuck else f'rm -f "{self.loaded}"'
        state = "spawn scheduled" if never_runs else "running"
        fake_tool(
            self.bin,
            "launchctl",
            f'''echo "launchctl $*" >> "{self.calls}"
case "$1" in
bootstrap) : > "{self.loaded}" ;;
bootout) {unload} ;;
print) [ -f "{self.loaded}" ] || exit 113; printf '\\tstate = {state}\\n\\t\\tstate = active\\n' ;;
esac
exit 0''',
        )

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def env(self, **extra: str) -> dict[str, str]:
        return {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin", **extra}

    def service(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/bin/bash", str(SERVICE), *args],
            capture_output=True,
            text=True,
            cwd=self.repo,
            env=self.env(**env),
            timeout=30,
        )

    def write_secrets(self) -> None:
        assert self.service("secrets", **SECRET_VALUES).returncode == 0

    def calls_made(self) -> str:
        return self.calls.read_text() if self.calls.exists() else ""

    @property
    def plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    return Kit(tmp_path)


# --- secrets ---------------------------------------------------------------------


def test_secrets_are_written_private_and_never_printed(kit: Kit) -> None:
    result = kit.service("secrets", **SECRET_VALUES)
    assert result.returncode == 0, result.stderr
    env_file = kit.t2c_home / "env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(kit.t2c_home.stat().st_mode) == 0o700
    for value in SECRET_VALUES.values():
        assert value in env_file.read_text()
        assert value not in result.stdout + result.stderr


def test_secrets_refuses_when_a_setting_is_missing_and_writes_nothing(kit: Kit) -> None:
    partial = {k: v for k, v in SECRET_VALUES.items() if k != "SLACK_APP_TOKEN"}
    result = kit.service("secrets", **partial)
    assert result.returncode != 0
    assert "SLACK_APP_TOKEN" in result.stderr
    assert not (kit.t2c_home / "env").exists()


def test_a_secret_with_shell_characters_survives_and_doesnt_run(kit: Kit, tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    tricky = {**SECRET_VALUES, "ANTHROPIC_API_KEY": f"a'b$(touch {marker})c d"}
    assert kit.service("secrets", **tricky).returncode == 0
    read = subprocess.run(
        ["/bin/sh", "-c", f'. "{kit.t2c_home / "env"}"; printf %s "$ANTHROPIC_API_KEY"'],
        capture_output=True,
        text=True,
    )
    assert read.stdout == tricky["ANTHROPIC_API_KEY"]
    assert not marker.exists()


# --- install ---------------------------------------------------------------------


def test_install_refuses_without_secrets_and_changes_nothing(kit: Kit) -> None:
    result = kit.service("install", "v0.1.0")
    assert result.returncode != 0
    assert "secrets" in result.stderr
    assert not kit.plist.exists()
    assert "launchctl" not in kit.calls_made()


@pytest.mark.parametrize("tag", ["v9.9.9", "main", "--upload-pack=x", "../v0.1.0", ""])
def test_install_refuses_anything_but_an_existing_tag(kit: Kit, tag: str) -> None:
    kit.write_secrets()
    result = kit.service("install", tag)
    assert result.returncode != 0
    assert not kit.plist.exists()
    assert not (kit.t2c_home / "releases").exists() or not list((kit.t2c_home / "releases").iterdir())
    assert "launchctl" not in kit.calls_made()


def test_install_puts_the_tag_in_its_release_folder_and_starts_the_service(kit: Kit) -> None:
    kit.write_secrets()
    result = kit.service("install", "v0.1.0")
    assert result.returncode == 0, result.stderr
    release = kit.t2c_home / "releases" / "v0.1.0"
    assert (release / "pyproject.toml").exists()
    assert not (release / ".git").exists()  # the tag's files, not a checkout
    assert (kit.t2c_home / "current").resolve() == release.resolve()
    calls = kit.calls_made()
    assert f"uv sync --locked --no-dev --project {release}" in calls
    assert f"launchctl bootstrap gui/{os.getuid()} {kit.plist}" in calls


def test_the_plist_runs_the_current_release_from_the_service_home(kit: Kit) -> None:
    kit.write_secrets()
    assert kit.service("install", "v0.1.0").returncode == 0
    plist = plistlib.loads(kit.plist.read_bytes())
    assert plist["Label"] == LABEL
    assert plist["ProgramArguments"] == ["/bin/sh", str(kit.t2c_home / "current" / "deploy" / "run-slack.sh")]
    assert plist["WorkingDirectory"] == str(kit.t2c_home)
    assert plist["KeepAlive"] is True  # a stray SIGTERM exits 0, and must not leave the bot offline
    assert plist["ExitTimeOut"] > 600  # a stage mid-call finishes first: the model call timeout is 600 s
    assert "/opt/homebrew/bin" in plist["EnvironmentVariables"]["PATH"]


def test_the_plist_holds_no_secret(kit: Kit) -> None:
    kit.write_secrets()
    assert kit.service("install", "v0.1.0").returncode == 0
    text = kit.plist.read_text()
    for name, value in SECRET_VALUES.items():
        assert value not in text
        assert name not in text


def test_installing_another_tag_switches_to_it_and_going_back_is_the_same_command(kit: Kit) -> None:
    kit.write_secrets()
    assert kit.service("install", "v0.1.0").returncode == 0
    (kit.repo / "new.txt").write_text("x")
    kit.git("add", ".")
    kit.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "two")
    kit.git("tag", "v0.2.0")
    assert kit.service("install", "v0.2.0").returncode == 0
    assert (kit.t2c_home / "current").resolve().name == "v0.2.0"
    assert f"launchctl bootout gui/{os.getuid()}/{LABEL}" in kit.calls_made()
    assert kit.service("install", "v0.1.0").returncode == 0
    assert (kit.t2c_home / "current").resolve().name == "v0.1.0"


def test_a_failed_dependency_install_keeps_the_running_release(kit: Kit) -> None:
    kit.write_secrets()
    assert kit.service("install", "v0.1.0").returncode == 0
    kit.git("tag", "v0.2.0")
    fake_tool(kit.bin, "uv", "exit 1")
    result = kit.service("install", "v0.2.0")
    assert result.returncode != 0
    assert (kit.t2c_home / "current").resolve().name == "v0.1.0"
    assert not (kit.t2c_home / "releases" / "v0.2.0").exists()


def test_a_broken_plist_is_refused_before_the_service_changes(kit: Kit) -> None:
    kit.write_secrets()
    fake_tool(kit.bin, "plutil", "exit 1")
    result = kit.service("install", "v0.1.0")
    assert result.returncode != 0
    assert "launchctl bootstrap" not in kit.calls_made()


def test_install_waits_for_the_old_service_to_stop_and_keeps_it_if_it_wont(kit: Kit) -> None:
    kit.write_secrets()
    assert kit.service("install", "v0.1.0").returncode == 0
    kit.git("tag", "v0.2.0")
    kit.fake_launchctl(stuck=True)
    result = kit.service("install", "v0.2.0", T2C_STOP_WAIT="1")
    assert result.returncode != 0
    assert "still stopping" in result.stderr
    assert (kit.t2c_home / "current").resolve().name == "v0.1.0"


def test_install_says_so_when_the_release_doesnt_start(kit: Kit) -> None:
    kit.write_secrets()
    kit.fake_launchctl(never_runs=True)
    result = kit.service("install", "v0.1.0", T2C_START_WAIT="1")
    assert result.returncode != 0
    assert "logs" in result.stderr
    assert "is running" not in result.stdout


def test_install_refuses_a_tag_without_the_service_script_before_stopping_anything(kit: Kit) -> None:
    kit.write_secrets()
    assert kit.service("install", "v0.1.0").returncode == 0
    (kit.repo / "deploy" / "run-slack.sh").unlink()
    kit.git("add", ".")
    kit.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "older")
    kit.git("tag", "v0.0.9")
    calls_before = kit.calls_made().count("bootout")
    result = kit.service("install", "v0.0.9")
    assert result.returncode != 0
    assert "run-slack.sh" in result.stderr
    assert kit.calls_made().count("bootout") == calls_before
    assert (kit.t2c_home / "current").resolve().name == "v0.1.0"
    assert not (kit.t2c_home / "releases" / "v0.0.9").exists()


def test_a_leftover_from_an_interrupted_install_doesnt_block_the_next(kit: Kit) -> None:
    kit.write_secrets()
    (kit.t2c_home / "releases" / "v0.1.0.new").mkdir(parents=True)
    assert kit.service("install", "v0.1.0").returncode == 0


def test_install_refuses_a_tag_that_moved(kit: Kit) -> None:
    kit.write_secrets()
    assert kit.service("install", "v0.1.0").returncode == 0
    (kit.repo / "new.txt").write_text("x")
    kit.git("add", ".")
    kit.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "two")
    kit.git("tag", "-f", "v0.1.0")
    result = kit.service("install", "v0.1.0")
    assert result.returncode != 0
    assert "moved" in result.stderr
    assert not (kit.t2c_home / "releases" / "v0.1.0" / "new.txt").exists()


# --- settings beyond the secrets --------------------------------------------------


def test_optional_settings_set_in_the_shell_reach_the_service(kit: Kit) -> None:
    assert kit.service("secrets", **SECRET_VALUES, FILM_BUDGET_USD="0.50", T2C_WATCHER="sdk").returncode == 0
    text = (kit.t2c_home / "env").read_text()
    assert "FILM_BUDGET_USD='0.50'" in text
    assert "T2C_WATCHER='sdk'" in text
    assert "T2C_WHISPER_MODEL" not in text


# --- a stale Spoiler List ---------------------------------------------------------


def test_status_warns_when_the_services_film_differs_from_the_repos(kit: Kit) -> None:
    assert kit.service("add-film", "demo-film").returncode == 0
    assert "differs" not in kit.service("status").stderr
    (kit.repo / "films" / "demo-film" / "film.toml").write_text("title='changed'\n")
    result = kit.service("status")
    assert "demo-film" in result.stderr
    assert "differs" in result.stderr


def test_install_warns_when_the_services_film_differs_from_the_repos(kit: Kit) -> None:
    kit.write_secrets()
    assert kit.service("add-film", "demo-film").returncode == 0
    (kit.repo / "films" / "demo-film" / "film.toml").write_text("title='changed'\n")
    result = kit.service("install", "v0.1.0")
    assert result.returncode == 0
    assert "demo-film" in result.stderr


def test_a_second_add_film_updates_the_list_and_keeps_what_the_service_cached(kit: Kit) -> None:
    assert kit.service("add-film", "demo-film").returncode == 0
    target = kit.t2c_home / "films" / "demo-film"
    (target / "frames").mkdir()
    (target / "frames" / "0001.jpg").write_text("cached")
    (kit.repo / "films" / "demo-film" / "film.toml").write_text("title='changed'\n")
    assert kit.service("add-film", "demo-film").returncode == 0
    assert (target / "film.toml").read_text() == "title='changed'\n"
    assert (target / "frames" / "0001.jpg").read_text() == "cached"


# --- add-film --------------------------------------------------------------------


def test_add_film_copies_one_film_into_the_service_home(kit: Kit) -> None:
    result = kit.service("add-film", "demo-film")
    assert result.returncode == 0, result.stderr
    copied = kit.t2c_home / "films" / "demo-film"
    assert (copied / "film.toml").exists()
    assert (copied / "source" / "trailer.webm").read_text() == "footage"


@pytest.mark.parametrize("slug", ["../secret", "a/b", "/etc", "..", ".", "-rf", "", "no-such-film", "a b", "My_Film"])
def test_add_film_refuses_a_slug_that_is_a_path_or_not_a_film(kit: Kit, slug: str) -> None:
    result = kit.service("add-film", slug)
    assert result.returncode != 0
    films = kit.t2c_home / "films"
    assert not films.exists() or not list(films.iterdir())


def test_add_film_needs_a_film_toml(kit: Kit) -> None:
    (kit.repo / "films" / "bare").mkdir()
    assert kit.service("add-film", "bare").returncode != 0


# --- status, logs, uninstall -----------------------------------------------------


def test_status_asks_launchd(kit: Kit) -> None:
    assert kit.service("status").returncode == 0
    assert f"launchctl print gui/{os.getuid()}/{LABEL}" in kit.calls_made()


def test_uninstall_stops_the_service_and_keeps_the_home(kit: Kit) -> None:
    kit.write_secrets()
    assert kit.service("install", "v0.1.0").returncode == 0
    assert kit.service("uninstall").returncode == 0
    assert not kit.plist.exists()
    assert f"launchctl bootout gui/{os.getuid()}/{LABEL}" in kit.calls_made()
    assert not (kit.t2c_home / "env").exists()  # credentials don't outlive the service
    assert (kit.t2c_home / "releases").exists()  # Run Logs and Films may be the only copy


def test_an_unknown_command_says_how_to_use_it(kit: Kit) -> None:
    result = kit.service("nonsense")
    assert result.returncode != 0
    assert "install" in result.stderr


# --- run-slack.sh ----------------------------------------------------------------


class Service:
    """run-slack.sh with a fake release beside it, run as launchd would."""

    def __init__(self, tmp: Path, kit: Kit) -> None:
        self.kit = kit
        self.log = tmp / "t2c.log"
        release = kit.t2c_home / "releases" / "v0.1.0"
        (release / ".venv" / "bin").mkdir(parents=True)
        (release / "deploy").mkdir()
        (release / "deploy" / "run-slack.sh").write_text(RUN_SLACK.read_text())
        fake_tool(
            release / ".venv" / "bin",
            "t2c",
            f'echo "args=$* cwd=$PWD key=$ANTHROPIC_API_KEY bot=$SLACK_BOT_TOKEN" > "{self.log}"\nexit 0',
        )
        fake_tool(kit.bin, "caffeinate", f'echo "caffeinate $*" >> "{kit.calls}"\nexit 0')
        (kit.t2c_home / "current").symlink_to(release)
        self.script = kit.t2c_home / "current" / "deploy" / "run-slack.sh"

    def run(self) -> subprocess.CompletedProcess:
        env = self.kit.env()
        return subprocess.run(
            ["/bin/sh", str(self.script)], capture_output=True, text=True, cwd=self.kit.t2c_home, env=env, timeout=30
        )


@pytest.fixture
def running(tmp_path: Path, kit: Kit) -> Service:
    kit.write_secrets()
    return Service(tmp_path, kit)


def test_the_service_starts_t2c_slack_from_its_release_with_its_secrets(running: Service) -> None:
    result = running.run()
    assert result.returncode == 0, result.stderr
    seen = running.log.read_text()
    assert "args=slack " in seen
    assert f"cwd={running.kit.t2c_home.resolve()}" in seen
    assert f"key={SECRET_VALUES['ANTHROPIC_API_KEY']}" in seen
    for _ in range(40):  # caffeinate starts in the background, beside the service
        if "caffeinate -i -w" in running.kit.calls_made():
            break
        time.sleep(0.05)
    assert "caffeinate -i -w" in running.kit.calls_made()  # awake while the service runs


def test_the_service_refuses_secrets_others_can_read(running: Service) -> None:
    (running.kit.t2c_home / "env").chmod(0o640)
    result = running.run()
    assert result.returncode != 0
    assert "env" in result.stderr
    assert not running.log.exists()


def test_the_service_names_a_missing_setting_and_never_a_value(running: Service) -> None:
    env_file = running.kit.t2c_home / "env"
    lines = [line for line in env_file.read_text().splitlines() if "SLACK_BOT_TOKEN" not in line]
    env_file.write_text("\n".join(lines) + "\n")
    result = running.run()
    assert result.returncode != 0
    assert "SLACK_BOT_TOKEN" in result.stderr
    for value in SECRET_VALUES.values():
        assert value not in result.stdout + result.stderr
    assert not running.log.exists()


def test_a_missing_secrets_file_refuses(running: Service) -> None:
    (running.kit.t2c_home / "env").unlink()
    result = running.run()
    assert result.returncode != 0
    assert not running.log.exists()
