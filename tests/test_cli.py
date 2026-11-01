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


class ClosableClient(str):
    """A stand-in client that compares as its name and can be closed, like the real one."""

    async def close(self) -> None:
        pass


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
        monkeypatch.setattr(cli, "make_client", lambda api_key: ClosableClient(f"client for {api_key}"))

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


def test_the_client_waits_no_longer_than_a_model_calls_deadline() -> None:
    from trailer_to_campaign.agents.loop import MODEL_CALL_TIMEOUT_S
    from trailer_to_campaign.cli import make_client

    timeout = make_client("sk-test").timeout
    assert timeout.read == MODEL_CALL_TIMEOUT_S
    assert timeout.connect == 5.0  # an unreachable API still fails at once, not after the deadline


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
        monkeypatch.setattr(cli, "make_client", lambda api_key: ClosableClient(f"client for {api_key}"))

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


def test_write_keeps_the_passed_posts_when_the_film_budget_runs_out_mid_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key_moments_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from decimal import Decimal

    from trailer_to_campaign.agents import review
    from trailer_to_campaign.agents.review import CheckedPost, FilmBudgetReachedMidReview
    from trailer_to_campaign.agents.social_posts import SocialPost
    from trailer_to_campaign.budget import FilmBudgetReached

    FakeCopywriter(monkeypatch)
    checked = [
        CheckedPost(SocialPost("facebook", "en", "passed post", []), "passed", [], 0),
        CheckedPost(SocialPost("facebook", "vi", "a rewrite", []), "flagged", ["this rewrite was never reviewed"], 1),
    ]

    async def runs_out(*args: Any, **kwargs: Any) -> Any:
        raise FilmBudgetReachedMidReview(FilmBudgetReached(Decimal("1.9"), Decimal("2"), Decimal("0.3")), checked)

    monkeypatch.setattr(review, "write_and_check", runs_out)
    assert main(write_argv(key_moments_file, tmp_path / "runs"), env=KEY_ENV) == EXIT_FILM_BUDGET_REACHED
    out, err = capsys.readouterr()
    assert "Film Budget reached" in err and "1 passed, 1 flagged" in out
    [saved] = (tmp_path / "runs").glob("agent-327/*/social_posts.json")
    assert [p["status"] for p in json.loads(saved.read_text())] == ["passed", "flagged"]
    assert watch_log(tmp_path / "runs")[-1]["stop_reason"] == "film_budget_reached"


# t2c run: one Run for a Film (the pipeline itself is tested in test_pipeline.py).


class FakePipeline:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, terminal: bool = True) -> None:
        from trailer_to_campaign import cli, pipeline

        self.calls: list[dict[str, Any]] = []
        monkeypatch.setattr(cli, "_at_a_terminal", lambda: terminal)
        monkeypatch.setattr(pipeline, "run_film", self)
        monkeypatch.setattr(
            pipeline, "real_stages", lambda film, source, settings, make: ("stages", film, source, make())
        )
        monkeypatch.setattr(cli, "make_client", lambda api_key: ClosableClient(f"client for {api_key}"))

    def __call__(self, film: str, budget: Any, runs_dir: Path, stages: Any, approver: Any) -> int:
        self.calls.append(
            {"film": film, "budget": budget, "runs_dir": runs_dir, "stages": stages, "approver": approver}
        )
        return 0


def test_run_needs_an_api_key_before_anything_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakePipeline(monkeypatch)
    argv = ["run", "--film", "agent-327", "--runs-dir", str(tmp_path / "runs")]
    assert main(argv, env={"T2C_FILMS_DIR": str(FILMS)}) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert fake.calls == []


def test_run_wires_one_run_with_a_terminal_approver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from decimal import Decimal

    from trailer_to_campaign.approval import TerminalApprover

    fake = FakePipeline(monkeypatch)
    argv = ["run", "--film", "agent-327", "--budget", "1.50", "--runs-dir", str(tmp_path / "runs")]
    assert main(argv, env=KEY_ENV) == 0
    [call] = fake.calls
    assert call["film"] == "agent-327" and call["budget"] == Decimal("1.50") and call["runs_dir"] == tmp_path / "runs"
    assert call["stages"] == (
        "stages",
        "agent-327",
        "https://www.youtube.com/watch?v=mN0zPOpADL4",
        "client for sk-test",
    )
    assert isinstance(call["approver"], TerminalApprover)  # a person decides; there is no other approver to choose


def test_run_needs_a_film_that_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePipeline(monkeypatch)
    assert main(["run", "--film", "agent-237", "--runs-dir", str(tmp_path / "runs")], env=KEY_ENV) == 2
    assert fake.calls == []


def test_run_refuses_approvals_that_are_not_typed_at_a_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`yes a | t2c run` must not approve every draft with no person at the keyboard."""
    fake = FakePipeline(monkeypatch, terminal=False)
    assert main(["run", "--film", "agent-327", "--runs-dir", str(tmp_path / "runs")], env=KEY_ENV) == 2
    assert "terminal" in capsys.readouterr().err
    assert fake.calls == [] and not (tmp_path / "runs").exists()


def test_every_command_shares_one_set_of_exit_codes() -> None:
    from trailer_to_campaign import cli, pipeline, run_log

    assert cli.EXIT_FILM_BUDGET_REACHED is pipeline.EXIT_FILM_BUDGET_REACHED is run_log.EXIT_FILM_BUDGET_REACHED
    assert cli.EXIT_AGENT_FAILED is pipeline.EXIT_AGENT_FAILED is run_log.EXIT_AGENT_FAILED


# t2c slack: the front door's process (the front door itself is tested in test_slack_app.py).

SLACK_ENV = {
    **KEY_ENV,
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_APP_TOKEN": "xapp-test",
    "T2C_MARKETERS": "U07MARKET",
}


@pytest.mark.parametrize("missing", ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "T2C_MARKETERS", "ANTHROPIC_API_KEY"])
def test_slack_refuses_to_start_without_its_settings(
    missing: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from trailer_to_campaign import slack_app

    monkeypatch.setattr(slack_app, "build_app", lambda *a: pytest.fail("must not start"))
    env = {k: v for k, v in SLACK_ENV.items() if k != missing}
    assert main(["slack"], env=env) == 2
    assert missing in capsys.readouterr().err


class FakeSocketMode:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import slack_bolt.adapter.socket_mode as socket_mode

        from trailer_to_campaign import slack_app

        self.doors: list[Any] = []
        self.tokens: list[str] = []
        monkeypatch.setattr(slack_app, "build_app", lambda token, door: self.doors.append(door) or ("app", token))
        fake = self

        class Handler:
            def __init__(self, app: Any, app_token: str) -> None:
                fake.tokens += [app[1], app_token]

            def start(self) -> None:
                raise KeyboardInterrupt  # someone pressed Ctrl-C

        monkeypatch.setattr(socket_mode, "SocketModeHandler", Handler)


def test_slack_runs_the_front_door_until_ctrl_c_and_closes_waiting_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = FakeSocketMode(monkeypatch)
    assert main(["slack", "--runs-dir", str(tmp_path / "runs")], env=SLACK_ENV) == 0
    assert socket.tokens == ["xoxb-test", "xapp-test"]
    [door] = socket.doors
    assert door.marketers == frozenset({"U07MARKET"})


def test_each_slack_run_is_a_full_run_for_the_requested_film(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from decimal import Decimal

    from trailer_to_campaign import pipeline
    from trailer_to_campaign.slack_request import RunRequest

    socket = FakeSocketMode(monkeypatch)
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(pipeline, "real_stages", lambda film, source, settings, make: ("stages", film, source))
    monkeypatch.setattr(
        pipeline,
        "run_film",
        lambda film, budget, runs_dir, stages, approver, out: runs.append(locals()) or 0,
    )
    main(["slack", "--budget", "1.50", "--runs-dir", str(tmp_path / "runs")], env=SLACK_ENV)
    [door] = socket.doors

    door.runner(RunRequest("agent-327", "https://youtu.be/x"), "approver", "thread")
    [run] = runs
    assert (run["film"], run["budget"], run["runs_dir"]) == ("agent-327", Decimal("1.50"), tmp_path / "runs")
    assert run["stages"] == ("stages", "agent-327", "https://youtu.be/x")
    assert (run["approver"], run["out"]) == ("approver", "thread")  # Approval and progress go to the Run Thread


# t2c eval: the wiring around the Eval Set (scoring is tested in test_evals.py).


def an_eval_set(tmp_path: Path) -> Path:
    from test_evals import ANSWER_KEY, film_toml

    root = tmp_path / "evals"
    (root / "films" / "alpha").mkdir(parents=True)
    (root / "films" / "alpha" / "film.toml").write_text(film_toml("Alpha"), encoding="utf-8")
    (root / "expected").mkdir()
    (root / "expected" / "alpha.toml").write_text(ANSWER_KEY, encoding="utf-8")
    (root / "eval_set.toml").write_text('films = ["alpha", "song-lang"]\n', encoding="utf-8")
    return root


def test_eval_needs_an_api_key_and_a_valid_eval_set_before_anything_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    eval_dir = an_eval_set(tmp_path)
    argv = ["eval", "--eval-dir", str(eval_dir), "--runs-dir", str(tmp_path / "runs")]
    assert main(argv, env={}) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    (eval_dir / "expected" / "alpha.toml").write_text("[[planted_drafts]]\nid = 1\n", encoding="utf-8")
    assert main(argv, env={"ANTHROPIC_API_KEY": "sk-test"}) == 2
    assert "alpha.toml" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()


def test_eval_runs_unattended_on_the_eval_sets_own_films(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_evals import FakeStages

    from trailer_to_campaign import cli, evals

    eval_dir = an_eval_set(tmp_path)
    fake, made = FakeStages(), []

    def stages(film: str, trailer: str, settings: Any, make_client: Any) -> Any:
        made.append((film, trailer, settings.films_dir, make_client()))
        return fake.stages(evals.EvalFilm(film, None, trailer))

    monkeypatch.setattr(evals, "real_stages", stages)
    monkeypatch.setattr(cli, "make_client", lambda api_key: f"client for {api_key}")
    monkeypatch.setattr(cli, "_at_a_terminal", lambda: False)  # piped stdin is fine: nothing asks for Approval
    argv = ["eval", "--eval-dir", str(eval_dir), "--runs-dir", str(tmp_path / "runs")]
    assert main(argv, env={"ANTHROPIC_API_KEY": "sk-test"}) == 0
    assert made == [("alpha", "https://www.youtube.com/watch?v=abc123", eval_dir / "films", "client for sk-test")]
    out = capsys.readouterr().out
    assert "Planted Drafts: caught 1 of 2" in out
    assert "song-lang" in out and "skipped" in out
    assert list((tmp_path / "runs").glob("alpha/*/run.jsonl"))


# T20: --only, --key-moments-from and --baseline.


def test_eval_only_drafts_reaches_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from trailer_to_campaign import evals

    seen: list[Any] = []
    monkeypatch.setattr(evals, "run_eval", lambda *args, **kwargs: seen.append(kwargs) or 0)
    argv = ["eval", "--eval-dir", str(an_eval_set(tmp_path)), "--runs-dir", str(tmp_path / "runs"), "--only", "drafts"]
    assert main(argv, env={"ANTHROPIC_API_KEY": "sk-test"}) == 0
    assert seen[0]["only_drafts"] is True and seen[0]["reused"] is None


def test_eval_key_moments_from_is_loaded_before_anything_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from trailer_to_campaign import evals

    seen: list[Any] = []
    monkeypatch.setattr(evals, "run_eval", lambda *args, **kwargs: seen.append(kwargs) or 0)
    base = ["eval", "--eval-dir", str(an_eval_set(tmp_path)), "--runs-dir", str(tmp_path / "runs")]

    missing = [*base, "--key-moments-from", str(tmp_path / "no-report")]
    assert main(missing, env={"ANTHROPIC_API_KEY": "sk-test"}) == 2
    assert "summary.json" in capsys.readouterr().err and seen == []

    loaded = object()
    monkeypatch.setattr(evals, "load_reused", lambda report, films: loaded)
    assert main(missing, env={"ANTHROPIC_API_KEY": "sk-test"}) == 0
    assert seen[0]["reused"] is loaded and seen[0]["only_drafts"] is False


@pytest.mark.parametrize(
    "flags",
    [
        ["--only", "drafts", "--key-moments-from", "x"],
        ["--only", "drafts", "--baseline"],
        ["--key-moments-from", "x", "--baseline"],
    ],
)
def test_eval_scope_flags_cannot_be_combined(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    with pytest.raises(SystemExit):
        main(["eval", "--eval-dir", str(an_eval_set(tmp_path)), *flags], env={"ANTHROPIC_API_KEY": "sk-test"})
    assert "not allowed with" in capsys.readouterr().err


def test_eval_only_accepts_drafts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["eval", "--only", "copywriter"], env={"ANTHROPIC_API_KEY": "sk-test"})
    assert "--only" in capsys.readouterr().err


def test_eval_baseline_is_free_and_needs_no_api_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["eval", "--baseline", "--eval-dir", str(an_eval_set(tmp_path)), "--runs-dir", str(tmp_path / "runs")]
    assert main(argv, env={}) == 1  # nothing saved yet
    assert "new run on main" in capsys.readouterr().out
    assert not (tmp_path / "runs").exists()  # looking writes nothing


def test_eval_baseline_compares_with_the_films_on_this_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from trailer_to_campaign import evals

    seen: list[Any] = []
    monkeypatch.setattr(evals, "show_baseline", lambda *args, **kwargs: seen.append(kwargs) or 0)
    argv = ["eval", "--baseline", "--eval-dir", str(an_eval_set(tmp_path)), "--runs-dir", str(tmp_path / "runs")]
    assert main(argv, env={}) == 0
    assert seen[0]["present"] == ["alpha"]  # song-lang is in the Eval Set but not on this clone


def test_eval_baseline_refuses_a_bad_eval_set(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    eval_dir = an_eval_set(tmp_path)
    (eval_dir / "expected" / "alpha.toml").write_text("[[planted_drafts]]\nid = 1\n", encoding="utf-8")
    assert main(["eval", "--baseline", "--eval-dir", str(eval_dir)], env={}) == 2
    assert "alpha.toml" in capsys.readouterr().err


@pytest.mark.parametrize(("flag", "expected"), [([], None), (["--parallel", "2"], 2)])
def test_eval_passes_how_many_films_run_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: list[str], expected: int | None
) -> None:
    from trailer_to_campaign import evals

    seen: list[Any] = []
    monkeypatch.setattr(evals, "run_eval", lambda *args, **kwargs: seen.append(kwargs.get("parallel")) or 0)
    argv = ["eval", "--eval-dir", str(an_eval_set(tmp_path)), "--runs-dir", str(tmp_path / "runs"), *flag]
    assert main(argv, env={"ANTHROPIC_API_KEY": "sk-test"}) == 0
    assert seen == [expected]


def test_eval_refuses_running_no_films_at_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["eval", "--eval-dir", str(an_eval_set(tmp_path)), "--parallel", "0"]
    with pytest.raises(SystemExit):
        main(argv, env={"ANTHROPIC_API_KEY": "sk-test"})
    assert "--parallel" in capsys.readouterr().err


# T11: --watcher picks the Watcher's harness for every command that watches a trailer.

WATCHER_CHOICES = [
    ([], {}, "v0"),
    (["--watcher", "sdk"], {}, "sdk"),
    ([], {"T2C_WATCHER": "sdk"}, "sdk"),
    (["--watcher", "v0"], {"T2C_WATCHER": "sdk"}, "v0"),  # the flag wins
]


@pytest.mark.parametrize(
    ("argv", "target"),
    [
        (["watch", "--film", "agent-327"], "watch"),
        (["run", "--film", "agent-327"], "run"),
        (["eval"], "run_eval_set"),
        (["slack"], "slack"),
    ],
)
@pytest.mark.parametrize(("flag", "env", "expected"), WATCHER_CHOICES)
def test_the_watcher_flag_reaches_the_command(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    target: str,
    flag: list[str],
    env: dict[str, str],
    expected: str,
) -> None:
    from trailer_to_campaign import cli

    seen: list[str] = []
    monkeypatch.setattr(cli, target, lambda **kwargs: seen.append(kwargs["settings"].watcher) or 0)
    assert main([*argv, *flag], env={"T2C_FILMS_DIR": str(FILMS), **env}) == 0
    assert seen == [expected]


def test_a_bad_t2c_watcher_never_blocks_the_flag_or_a_command_without_a_watcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trailer_to_campaign import cli

    seen: list[str] = []
    monkeypatch.setattr(cli, "watch", lambda **kwargs: seen.append(kwargs["settings"].watcher) or 0)
    bad = {"T2C_FILMS_DIR": str(FILMS), "T2C_WATCHER": "SDK"}
    assert main(["watch", "--film", "agent-327", "--watcher", "v0"], env=bad) == 0  # the flag wins
    assert seen == ["v0"]
    assert main(["dummy-run", "--calls", "1", "--runs-dir", str(tmp_path)], env=bad) == 0  # no Watcher at all


def test_an_unknown_watcher_flag_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["watch", "--film", "agent-327", "--watcher", "v2"])
    assert "invalid choice" in capsys.readouterr().err


# T12: the side-by-side result, from the Film's Run Logs and the two stopwatch times.


def test_result_writes_the_page_without_an_api_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from test_result import FILM, FILM_TOML, trial

    films = tmp_path / "films"
    (films / FILM).mkdir(parents=True)
    (films / FILM / "film.toml").write_text(FILM_TOML, encoding="utf-8")
    trial(tmp_path / "runs")
    argv = ["result", "--film", FILM, "--old-way", "240", "--new-way", "60", "--runs-dir", str(tmp_path / "runs")]
    assert main(argv, env={"T2C_FILMS_DIR": str(films)}) == 0
    out = capsys.readouterr().out
    assert "Time Saved: 180 min (75%)" in out and f"{FILM}/result.md and result.json under the --runs-dir" in out
    assert str(tmp_path) not in out  # no local paths printed
    assert (tmp_path / "runs" / FILM / "result.md").is_file()


def test_result_names_a_relative_runs_folder_as_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_result import FILM, FILM_TOML, trial

    monkeypatch.chdir(tmp_path)
    (tmp_path / "films" / FILM).mkdir(parents=True)
    (tmp_path / "films" / FILM / "film.toml").write_text(FILM_TOML, encoding="utf-8")
    trial(tmp_path / "runs")
    argv = ["result", "--film", FILM, "--old-way", "240", "--new-way", "60", "--runs-dir", "runs"]
    assert main(argv, env={"T2C_FILMS_DIR": "films"}) == 0
    assert f"runs/{FILM}/result.md and result.json" in capsys.readouterr().out


def test_result_that_cannot_be_written_says_so_without_a_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_result import FILM, FILM_TOML, trial

    from trailer_to_campaign import result

    def read_only(*args: Any) -> Any:
        raise PermissionError(13, "Permission denied", str(tmp_path / "runs" / FILM / "result.md"))

    monkeypatch.setattr(result, "write_result", read_only)
    (tmp_path / "films" / FILM).mkdir(parents=True)
    (tmp_path / "films" / FILM / "film.toml").write_text(FILM_TOML, encoding="utf-8")
    trial(tmp_path / "runs")
    argv = ["result", "--film", FILM, "--old-way", "240", "--new-way", "60", "--runs-dir", str(tmp_path / "runs")]
    assert main(argv, env={"T2C_FILMS_DIR": str(tmp_path / "films")}) == 2
    err = capsys.readouterr().err
    assert "PermissionError" in err and str(tmp_path) not in err


@pytest.mark.parametrize("old", ["0", "soon"])
def test_result_refuses_bad_minutes(tmp_path: Path, old: str, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["result", "--film", "agent-327", "--old-way", old, "--new-way", "60", "--runs-dir", str(tmp_path)]
    assert main(argv, env={"T2C_FILMS_DIR": str(FILMS)}) == 2
    assert "old-way" in capsys.readouterr().err
