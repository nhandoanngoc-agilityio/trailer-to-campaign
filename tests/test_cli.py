"""Tests for the t2c command line."""

from __future__ import annotations

import json
import os
import subprocess
from decimal import Decimal
from pathlib import Path

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
