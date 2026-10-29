"""Tests for the t2c command line."""

from __future__ import annotations

import json
import os
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trailer_to_campaign.budget import MeteredCall
from trailer_to_campaign.cli import EXIT_FILM_BUDGET_REACHED, main
from trailer_to_campaign.pricing import Usage


def run_log_lines(runs_dir: Path) -> list[dict]:
    [path] = runs_dir.glob("sample/*/run.jsonl")
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_dummy_run_stops_at_a_tiny_budget(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["dummy-run", "--film", "sample", "--budget", "0.001", "--runs-dir", str(tmp_path)], env={})
    assert code == EXIT_FILM_BUDGET_REACHED
    assert "Film Budget reached: spent $" in capsys.readouterr().err
    last = run_log_lines(tmp_path)[-1]
    assert last["event"] == "run_finished"
    assert last["stop_reason"] == "film_budget_reached"
    assert Decimal(last["total_usd"]) <= Decimal("0.001")


def test_dummy_run_completes_within_a_normal_budget(tmp_path: Path) -> None:
    code = main(["dummy-run", "--film", "sample", "--calls", "3", "--runs-dir", str(tmp_path)], env={})
    assert code == 0
    lines = run_log_lines(tmp_path)
    calls = [line for line in lines if line["event"] == "model_call"]
    assert len(calls) == 3
    assert lines[-1]["stop_reason"] == "completed"
    assert Decimal(lines[-1]["total_usd"]) == sum(Decimal(c["cost_usd"]) for c in calls)


def test_budget_comes_from_the_environment(tmp_path: Path) -> None:
    code = main(["dummy-run", "--runs-dir", str(tmp_path)], env={"FILM_BUDGET_USD": "0.001"})
    assert code == EXIT_FILM_BUDGET_REACHED


def test_bad_budget_is_a_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as err:
        main(["dummy-run", "--budget", "-5", "--runs-dir", str(tmp_path)], env={})
    assert err.value.code == 2
    assert "--budget" in capsys.readouterr().err


def test_bad_budget_in_environment_is_a_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as err:
        main(["dummy-run", "--runs-dir", str(tmp_path)], env={"FILM_BUDGET_USD": "lots"})
    assert err.value.code == 2
    assert "FILM_BUDGET_USD" in capsys.readouterr().err


def test_budget_flag_overrides_a_bad_environment_value(tmp_path: Path) -> None:
    code = main(
        ["dummy-run", "--budget", "1.00", "--calls", "1", "--runs-dir", str(tmp_path)], env={"FILM_BUDGET_USD": "x"}
    )
    assert code == 0


def test_an_interrupted_run_still_finishes_its_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt(self: MeteredCall, usage: Usage) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(MeteredCall, "record", interrupt)
    with pytest.raises(KeyboardInterrupt):
        main(["dummy-run", "--runs-dir", str(tmp_path)], env={})
    last = run_log_lines(tmp_path)[-1]
    assert last["event"] == "run_finished"
    assert last["stop_reason"] == "error"
    assert Decimal(last["total_usd"]) > 0  # the interrupted call was charged its worst case


def test_t2c_is_installed_as_a_command(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    result = subprocess.run(
        ["t2c", "dummy-run", "--budget", "0.001", "--runs-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == EXIT_FILM_BUDGET_REACHED
    assert "Film Budget reached" in result.stderr


def test_serve_footage_runs_the_footage_server_on_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp.server.mcpserver import MCPServer

    served: list[tuple[str, tuple]] = []
    monkeypatch.setattr(MCPServer, "run", lambda self, *args, **kw: served.append((self.name, args)))
    assert main(["serve-footage"], env={}) == 0
    assert served == [("footage", ())]


def test_serve_footage_uses_an_absolute_films_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # An MCP host may start the server in / or $HOME, so films/ must not depend on the cwd.
    from mcp.server.mcpserver import MCPServer

    from trailer_to_campaign.tools import footage

    seen: list[Path] = []
    real = footage.create_server
    monkeypatch.setattr(
        footage, "create_server", lambda films_dir, **kw: seen.append(films_dir) or real(films_dir, **kw)
    )
    monkeypatch.setattr(MCPServer, "run", lambda self, *args, **kw: None)
    (tmp_path / "films").mkdir()
    main(["serve-footage"], env={"T2C_FILMS_DIR": str(tmp_path / "films")})
    main(["serve-footage"], env={})
    assert seen[0] == tmp_path / "films"
    assert seen[1].is_absolute()


@pytest.mark.parametrize(("command", "server"), [("serve-catalog", "catalog"), ("serve-spoilers", "spoilers")])
def test_film_servers_run_on_stdio_with_an_absolute_films_folder(
    monkeypatch: pytest.MonkeyPatch, command: str, server: str
) -> None:
    from mcp.server.mcpserver import MCPServer

    from trailer_to_campaign.tools import catalog, spoilers

    served: list[str] = []
    dirs: list[Path] = []
    monkeypatch.setattr(MCPServer, "run", lambda self, *args, **kw: served.append(self.name))
    for module, name in ((catalog, "create_catalog_server"), (spoilers, "create_spoiler_server")):
        real = getattr(module, name)
        monkeypatch.setattr(module, name, lambda films_dir, _real=real: dirs.append(films_dir) or _real(films_dir))
    assert main([command], env={}) == 0
    assert served == [server]
    assert dirs[0].is_absolute()


@pytest.mark.parametrize("command", ["serve-footage", "serve-catalog", "serve-spoilers"])
def test_servers_refuse_to_start_without_a_films_folder(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An MCP host may start the server in $HOME; serving from a folder that isn't there helps no one.
    from mcp.server.mcpserver import MCPServer

    monkeypatch.setattr(MCPServer, "run", lambda self, *args, **kw: pytest.fail("must not start"))
    assert main([command], env={"T2C_FILMS_DIR": str(tmp_path / "missing")}) == 2
    assert "T2C_FILMS_DIR" in capsys.readouterr().err


# t2c watch: the wiring around the Watcher (the Watcher itself is tested in test_watcher.py).

FILMS = Path(__file__).resolve().parent.parent / "films"
KEY_ENV = {"ANTHROPIC_API_KEY": "sk-test", "T2C_FILMS_DIR": str(FILMS)}


def a_moment(start: float) -> Any:
    from trailer_to_campaign.agents.key_moments import KeyMoment, Quote

    return KeyMoment(
        start, start + 2, "Agent 327 walks in.", "deadpan", Quote("Hello.", start, start + 1), ["Agent 327"], [3]
    )


class FakeWatcher:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, outcome: Any = None) -> None:
        from trailer_to_campaign import cli
        from trailer_to_campaign.agents import watcher

        self.calls: list[dict[str, Any]] = []
        self.outcome = outcome
        monkeypatch.setattr(watcher, "watch_trailer", self)
        monkeypatch.setattr(watcher, "watcher_servers", lambda settings: ["footage", "catalog"])
        monkeypatch.setattr(cli, "make_client", lambda api_key: f"client for {api_key}")

    async def __call__(self, film: str, source: str, *, servers: Any, client: Any, meter: Any) -> Any:
        self.calls.append({"film": film, "source": source, "servers": servers, "client": client})
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if self.outcome == "spend":
            with meter.call("claude-opus-5-5", 50_000, 16_000, agent="watcher") as call:
                call.record(Usage(input=50_000, output=1_000))
        return [a_moment(t) for t in (0.0, 10.0, 20.0, 30.0, 40.0)]


def watch_log(runs_dir: Path) -> list[dict]:
    [path] = runs_dir.glob("agent-327/*/run.jsonl")
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_watch_needs_an_api_key_before_anything_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeWatcher(monkeypatch)
    code = main(["watch", "--film", "agent-327", "--runs-dir", str(tmp_path)], env={"T2C_FILMS_DIR": str(FILMS)})
    assert code == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert fake.calls == [] and not any(tmp_path.iterdir())


def test_watch_writes_key_moments_next_to_the_run_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeWatcher(monkeypatch, outcome="spend")
    code = main(["watch", "--film", "agent-327", "--runs-dir", str(tmp_path)], env=KEY_ENV)
    assert code == 0
    [call] = fake.calls
    assert call == {
        "film": "agent-327",
        "source": "https://www.youtube.com/watch?v=mN0zPOpADL4",  # the catalog's trailer link
        "servers": ["footage", "catalog"],
        "client": "client for sk-test",
    }
    [saved] = tmp_path.glob("agent-327/*/key_moments.json")
    moments = json.loads(saved.read_text())
    assert len(moments) == 5
    assert moments[0]["quote"] == {"text": "Hello.", "start": 0.0, "end": 1.0}
    lines = watch_log(tmp_path)
    assert lines[-1]["event"] == "run_finished" and lines[-1]["stop_reason"] == "completed"
    assert Decimal(lines[-1]["total_usd"]) > 0
    assert "5 Key Moments" in capsys.readouterr().out


def test_watch_takes_a_source_from_the_command_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeWatcher(monkeypatch)
    argv = ["watch", "--film", "agent-327", "--source", "trailer.mp4", "--runs-dir", str(tmp_path)]
    assert main(argv, env=KEY_ENV) == 0
    assert fake.calls[0]["source"] == "trailer.mp4"


def test_watch_stops_at_the_film_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeWatcher(monkeypatch, outcome="spend")
    argv = ["watch", "--film", "agent-327", "--budget", "0.05", "--runs-dir", str(tmp_path)]
    assert main(argv, env=KEY_ENV) == EXIT_FILM_BUDGET_REACHED
    assert "Film Budget reached" in capsys.readouterr().err
    assert watch_log(tmp_path)[-1]["stop_reason"] == "film_budget_reached"
    assert list(tmp_path.glob("agent-327/*/key_moments.json")) == []


def test_watch_reports_a_watcher_that_gave_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from trailer_to_campaign.agents.loop import AgentFailed

    FakeWatcher(monkeypatch, outcome=AgentFailed("the watcher did not finish in 30 turns"))
    assert main(["watch", "--film", "agent-327", "--runs-dir", str(tmp_path)], env=KEY_ENV) == 1
    assert "30 turns" in capsys.readouterr().err
    assert watch_log(tmp_path)[-1]["stop_reason"] == "error"


def test_watch_needs_a_trailer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    film = tmp_path / "films" / "agent-327"
    film.mkdir(parents=True)
    toml = (FILMS / "agent-327" / "film.toml").read_text()
    (film / "film.toml").write_text("\n".join(line for line in toml.splitlines() if not line.startswith("trailer =")))
    fake = FakeWatcher(monkeypatch)
    env = {**KEY_ENV, "T2C_FILMS_DIR": str(tmp_path / "films")}
    assert main(["watch", "--film", "agent-327", "--runs-dir", str(tmp_path / "runs")], env=env) == 2
    assert "--source" in capsys.readouterr().err
    assert fake.calls == []


def test_the_watchers_client_never_retries_a_call_on_its_own() -> None:
    from trailer_to_campaign.cli import make_client

    client = make_client("sk-test")
    assert client.max_retries == 0  # a retried call could be billed twice but metered once


def test_key_moments_that_cannot_be_saved_are_printed_not_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeWatcher(monkeypatch, outcome="spend")
    real_call = fake.__call__

    async def blocked(*args: Any, meter: Any, **kwargs: Any) -> Any:
        (meter.run_log.path.parent / "key_moments.json").mkdir()  # writing the file will fail
        return await real_call(*args, meter=meter, **kwargs)

    monkeypatch.setattr("trailer_to_campaign.agents.watcher.watch_trailer", blocked)
    assert main(["watch", "--film", "agent-327", "--runs-dir", str(tmp_path)], env=KEY_ENV) == 1
    out, err = capsys.readouterr()
    assert len(json.loads(out)) == 5
    assert "could not save" in err
    assert watch_log(tmp_path)[-1]["stop_reason"] == "error"


def test_a_failure_setting_up_the_watcher_still_ends_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeWatcher(monkeypatch)

    def no_servers(settings: Any) -> Any:
        raise RuntimeError("no ffmpeg")

    monkeypatch.setattr("trailer_to_campaign.agents.watcher.watcher_servers", no_servers)
    with pytest.raises(RuntimeError, match="no ffmpeg"):
        main(["watch", "--film", "agent-327", "--runs-dir", str(tmp_path)], env=KEY_ENV)
    assert watch_log(tmp_path)[-1]["stop_reason"] == "error"


# t2c write: the wiring around the Copywriter (the Copywriter itself is tested in test_copywriter.py).


class FakeCopywriter:
    """Stands in for the Copywriter -> Checker loop: t2c write must run the whole loop, never the writer alone."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, spend: bool = False, flag: bool = False) -> None:
        from trailer_to_campaign import cli
        from trailer_to_campaign.agents import review

        self.calls: list[dict[str, Any]] = []
        self.spend, self.flag = spend, flag
        monkeypatch.setattr(review, "write_and_check", self)
        monkeypatch.setattr(review, "agents_for", lambda film, settings, client, meter: ("agents", film, client))
        monkeypatch.setattr(cli, "make_client", lambda api_key: f"client for {api_key}")

    async def __call__(self, key_moments: Any, agents: Any, meter: Any, first_draft: Any = None) -> Any:
        from trailer_to_campaign.agents.review import CheckedPost
        from trailer_to_campaign.agents.social_posts import Claim, SocialPost

        self.calls.append({"key_moments": key_moments, "agents": agents, "first_draft": first_draft})
        if self.spend:
            with meter.call("claude-opus-5-5", 50_000, 16_000, agent="copywriter") as call:
                call.record(Usage(input=50_000, output=1_000))
        claims = [Claim("The title", "catalog:title")]
        checked = [
            CheckedPost(SocialPost(p, lang, f"{p} {lang}", claims), "passed", [], 0)
            for p in ("facebook", "instagram", "tiktok")
            for lang in ("en", "vi")
        ]
        if self.flag:
            checked[0] = CheckedPost(checked[0].post, "flagged", ["spoiler ending-capture: reveals the ending"], 2)
        return checked


@pytest.fixture
def key_moments_file(tmp_path: Path) -> Path:
    return watched(tmp_path, "agent-327")


def watched(tmp_path: Path, film: str) -> Path:
    """A key_moments.json where t2c watch writes it: <runs>/<film>/<time>/."""
    from dataclasses import asdict

    path = tmp_path / "watched" / film / "20261029T145957Z" / "key_moments.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([asdict(a_moment(t)) for t in (0.0, 10.0, 20.0, 30.0, 40.0)]), encoding="utf-8")
    return path


def write_argv(key_moments: Path, runs_dir: Path, *extra: str) -> list[str]:
    return ["write", "--film", "agent-327", "--key-moments", str(key_moments), "--runs-dir", str(runs_dir), *extra]


def test_write_needs_an_api_key_before_anything_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key_moments_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCopywriter(monkeypatch)
    assert main(write_argv(key_moments_file, tmp_path / "runs"), env={"T2C_FILMS_DIR": str(FILMS)}) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert fake.calls == [] and not (tmp_path / "runs").exists()


@pytest.mark.parametrize("content", [None, "not json"])
def test_write_needs_readable_key_moments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], content: str | None
) -> None:
    fake = FakeCopywriter(monkeypatch)
    path = tmp_path / "key_moments.json"
    if content is not None:
        path.write_text(content)
    assert main(write_argv(path, tmp_path / "runs"), env=KEY_ENV) == 2
    assert "key_moments.json" in capsys.readouterr().err
    assert fake.calls == [] and not (tmp_path / "runs").exists()


def test_write_saves_social_posts_next_to_the_run_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key_moments_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCopywriter(monkeypatch, spend=True)
    assert main(write_argv(key_moments_file, tmp_path / "runs"), env=KEY_ENV) == 0
    [call] = fake.calls
    assert len(call["key_moments"]) == 5 and call["first_draft"] is None  # the CLI never plants a draft
    assert call["agents"] == ("agents", "agent-327", "client for sk-test")
    [saved] = (tmp_path / "runs").glob("agent-327/*/social_posts.json")
    posts = json.loads(saved.read_text())
    assert len(posts) == 6 and {p["status"] for p in posts} == {"passed"}
    assert posts[0]["post"]["claims"] == [{"text": "The title", "source": "catalog:title"}]
    lines = watch_log(tmp_path / "runs")
    assert lines[-1]["stop_reason"] == "completed"
    assert any(line["event"] == "social_posts" and line["count"] == 6 for line in lines)
    assert "6 Social Posts" in capsys.readouterr().out


def test_write_stops_at_the_film_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key_moments_file: Path
) -> None:
    FakeCopywriter(monkeypatch, spend=True)
    assert main(write_argv(key_moments_file, tmp_path / "runs", "--budget", "0.05"), env=KEY_ENV) == 3
    assert watch_log(tmp_path / "runs")[-1]["stop_reason"] == "film_budget_reached"


def test_write_refuses_a_film_that_does_not_exist_before_any_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCopywriter(monkeypatch)
    argv = ["write", "--film", "agent-237", "--key-moments", str(watched(tmp_path, "agent-237"))]
    assert main([*argv, "--runs-dir", str(tmp_path / "runs")], env=KEY_ENV) == 2
    assert "agent-237" in capsys.readouterr().err
    assert fake.calls == [] and not (tmp_path / "runs").exists()


def test_write_refuses_key_moments_watched_for_another_film(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCopywriter(monkeypatch)
    assert main(write_argv(watched(tmp_path, "the-quiet-river"), tmp_path / "runs"), env=KEY_ENV) == 2
    assert "the-quiet-river" in capsys.readouterr().err
    assert fake.calls == [] and not (tmp_path / "runs").exists()


def test_write_completes_with_flagged_posts_and_shows_their_problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key_moments_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeCopywriter(monkeypatch, flag=True)
    assert main(write_argv(key_moments_file, tmp_path / "runs"), env=KEY_ENV) == 0  # flagged posts go to a person
    out = capsys.readouterr().out
    assert "flagged" in out and "reveals the ending" in out and "5 passed, 1 flagged" in out
    [saved] = (tmp_path / "runs").glob("agent-327/*/social_posts.json")
    assert json.loads(saved.read_text())[0]["status"] == "flagged"
