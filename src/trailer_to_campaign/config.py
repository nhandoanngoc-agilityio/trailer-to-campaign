"""Settings, read from the environment. Secrets never come from the repo.

ANTHROPIC_API_KEY   needed only by commands that call a model
FILM_BUDGET_USD     the Film Budget for one Run, default 2.00
T2C_WHISPER_MODEL   the faster-whisper model for transcripts, default small
T2C_FILMS_DIR       where the films/ folder is, default films (relative to the cwd)
SLACK_BOT_TOKEN     the Slack app's bot token (xoxb-), for t2c slack
SLACK_APP_TOKEN     the Slack app's Socket Mode token (xapp-), for t2c slack
T2C_MARKETERS       the Slack user ids of the Marketers, comma-separated: only they start Runs and give Approval
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

DEFAULT_FILM_BUDGET_USD = Decimal("2.00")
DEFAULT_WHISPER_MODEL = "small"


class MissingApiKey(RuntimeError):
    def __str__(self) -> str:
        return "ANTHROPIC_API_KEY is not set; export it before running a command that calls a model"


class MissingSlackSettings(RuntimeError):
    def __init__(self, missing: list[str]) -> None:
        super().__init__(f"{', '.join(missing)} not set; export them before running t2c slack")


def parse_film_budget(value: str, source: str = "FILM_BUDGET_USD") -> Decimal:
    try:
        budget = Decimal(value)
    except InvalidOperation:
        budget = None
    if budget is None or not budget.is_finite() or budget <= 0:
        raise ValueError(f"{source} must be a positive dollar amount like 2.00, got {value!r}")
    return budget


@dataclass(frozen=True)
class Settings:
    film_budget_usd: Decimal = DEFAULT_FILM_BUDGET_USD
    runs_dir: Path = Path("runs")
    films_dir: Path = Path("films")
    whisper_model: str = DEFAULT_WHISPER_MODEL
    marketers: frozenset[str] = frozenset()  # Slack user ids: only they start Runs and give Approval
    _api_key: str | None = field(default=None, repr=False)
    _slack_bot_token: str | None = field(default=None, repr=False)
    _slack_app_token: str | None = field(default=None, repr=False)

    def require_api_key(self) -> str:
        if not self._api_key:
            raise MissingApiKey()
        return self._api_key

    def require_slack(self) -> tuple[str, str]:
        """The bot and Socket Mode tokens; the Slack app also needs at least one Marketer."""
        missing = [
            name
            for name, value in (
                ("SLACK_BOT_TOKEN", self._slack_bot_token),
                ("SLACK_APP_TOKEN", self._slack_app_token),
                ("T2C_MARKETERS", self.marketers),
            )
            if not value
        ]
        if missing:
            raise MissingSlackSettings(missing)
        assert self._slack_bot_token and self._slack_app_token
        return self._slack_bot_token, self._slack_app_token


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    budget = env.get("FILM_BUDGET_USD")
    return Settings(
        film_budget_usd=DEFAULT_FILM_BUDGET_USD if budget is None else parse_film_budget(budget),
        films_dir=Path(env.get("T2C_FILMS_DIR") or "films"),
        whisper_model=env.get("T2C_WHISPER_MODEL") or DEFAULT_WHISPER_MODEL,
        marketers=frozenset(m.strip() for m in (env.get("T2C_MARKETERS") or "").split(",") if m.strip()),
        _api_key=env.get("ANTHROPIC_API_KEY") or None,
        _slack_bot_token=env.get("SLACK_BOT_TOKEN") or None,
        _slack_app_token=env.get("SLACK_APP_TOKEN") or None,
    )
