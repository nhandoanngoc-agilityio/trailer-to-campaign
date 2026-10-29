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


def test_films_folder_can_be_set() -> None:
    assert load_settings({"T2C_FILMS_DIR": "/srv/films"}).films_dir == Path("/srv/films")
