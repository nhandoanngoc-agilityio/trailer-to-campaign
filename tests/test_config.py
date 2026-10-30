"""Tests for settings read from the environment."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from trailer_to_campaign.config import MissingApiKey, load_settings

KEY = "sk-ant-test-0123456789"


def test_default_film_budget_is_two_dollars() -> None:
    assert load_settings({}).film_budget_usd == Decimal("2.00")


def test_film_budget_from_environment() -> None:
    assert load_settings({"FILM_BUDGET_USD": "0.75"}).film_budget_usd == Decimal("0.75")


@pytest.mark.parametrize("value", ["abc", "0", "-1", "nan", "inf", ""])
def test_bad_film_budget_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="FILM_BUDGET_USD"):
        load_settings({"FILM_BUDGET_USD": value})


def test_dirs_default_to_the_repo_layout() -> None:
    settings = load_settings({})
    assert settings.runs_dir == Path("runs")
    assert settings.films_dir == Path("films")


def test_api_key_is_required_only_when_asked_for() -> None:
    settings = load_settings({})
    with pytest.raises(MissingApiKey, match="ANTHROPIC_API_KEY"):
        settings.require_api_key()


def test_api_key_is_read_from_environment() -> None:
    assert load_settings({"ANTHROPIC_API_KEY": KEY}).require_api_key() == KEY


def test_api_key_never_shows_in_repr() -> None:
    settings = load_settings({"ANTHROPIC_API_KEY": KEY})
    assert KEY not in repr(settings)
    assert KEY not in str(settings)


# T9: the Slack app's tokens and the Marketers, all from the environment.

SLACK = {"SLACK_BOT_TOKEN": "xoxb-secret-bot", "SLACK_APP_TOKEN": "xapp-secret-app", "T2C_MARKETERS": "U07A, U07B,,"}


def test_marketers_are_a_list_of_slack_ids() -> None:
    assert load_settings(SLACK).marketers == frozenset({"U07A", "U07B"})
    assert load_settings({}).marketers == frozenset()


def test_slack_tokens_are_read_and_never_shown() -> None:
    settings = load_settings(SLACK)
    assert settings.require_slack() == ("xoxb-secret-bot", "xapp-secret-app")
    assert "secret" not in repr(settings)


@pytest.mark.parametrize("missing", ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "T2C_MARKETERS"])
def test_the_slack_app_needs_both_tokens_and_a_marketer(missing: str) -> None:
    from trailer_to_campaign.config import MissingSlackSettings

    env = {k: v for k, v in SLACK.items() if k != missing}
    with pytest.raises(MissingSlackSettings, match=missing):
        load_settings(env).require_slack()


def test_films_folder_can_be_set() -> None:
    assert load_settings({"T2C_FILMS_DIR": "/srv/films"}).films_dir == Path("/srv/films")


# T11: which Watcher runs: v0's own loop, or the Agent SDK.


def test_the_watcher_is_v0_unless_set() -> None:
    assert load_settings({}).watcher == "v0"
    assert load_settings({"T2C_WATCHER": "sdk"}).watcher == "sdk"


def test_an_unknown_watcher_is_rejected() -> None:
    with pytest.raises(ValueError, match="T2C_WATCHER"):
        load_settings({"T2C_WATCHER": "v2"})
