"""Settings, read from the environment. Secrets never come from the repo.

ANTHROPIC_API_KEY   needed only by commands that call a model
FILM_BUDGET_USD     the Film Budget for one Run, default 2.00
T2C_WHISPER_MODEL   the faster-whisper model for transcripts, default small
T2C_FILMS_DIR       where the films/ folder is, default films (relative to the cwd)
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
    _api_key: str | None = field(default=None, repr=False)

    def require_api_key(self) -> str:
        if not self._api_key:
            raise MissingApiKey()
        return self._api_key


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    budget = env.get("FILM_BUDGET_USD")
    return Settings(
        film_budget_usd=DEFAULT_FILM_BUDGET_USD if budget is None else parse_film_budget(budget),
        films_dir=Path(env.get("T2C_FILMS_DIR") or "films"),
        whisper_model=env.get("T2C_WHISPER_MODEL") or DEFAULT_WHISPER_MODEL,
        _api_key=env.get("ANTHROPIC_API_KEY") or None,
    )
