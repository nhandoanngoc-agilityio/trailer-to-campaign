"""Model prices the Film Budget meter charges from.

Standard first-party API rates in USD per million tokens, taken from
https://platform.claude.com/docs/en/about-claude/pricing on 2026-10-29.
Fast mode, the Batch API and US-only `inference_geo` (1.1x) are not priced:
nothing in slice 1 uses them, so a call that did would be under-charged.
A model missing from PRICES is refused, never guessed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import Any

MTOK = 1_000_000

# Claude Haiku 5.5 charges higher prices for a request whose prompt (all input
# tokens, cache reads and writes included) is over this many tokens.
HAIKU_LONG_PROMPT_TOKENS = 100_000


class UnknownModel(KeyError):
    """The model has no entry in PRICES, so its cost can't be metered."""

    def __str__(self) -> str:
        return f"no price for model {self.args[0]!r}; add it to pricing.PRICES before calling it"


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: Decimal
    cache_write_5m: Decimal
    cache_write_1h: Decimal
    cache_read: Decimal
    output: Decimal

    @property
    def max_input_side(self) -> Decimal:
        return max(self.input, self.cache_write_5m, self.cache_write_1h, self.cache_read)


def _price(input: str, cache_write_5m: str, cache_write_1h: str, cache_read: str, output: str) -> Price:
    return Price(*(Decimal(v) for v in (input, cache_write_5m, cache_write_1h, cache_read, output)))


PRICES: dict[str, Price] = {
    "claude-fable-5-1": _price("10", "12.50", "20", "0.25", "50"),
    "claude-opus-5-5": _price("4", "5", "8", "0.20", "20"),
    "claude-sonnet-5-5": _price("2", "2.50", "4", "0.10", "10"),
    "claude-haiku-5-5": _price("0.10", "0.125", "0.20", "0.01", "0.50"),
}

# Prices for models whose long prompts cost more, keyed like PRICES.
LONG_PROMPT_PRICES: dict[str, tuple[int, Price]] = {
    "claude-haiku-5-5": (HAIKU_LONG_PROMPT_TOKENS, _price("0.50", "0.625", "1", "0.05", "2.50")),
}


@dataclass(frozen=True)
class Usage:
    """Token counts for one model call, as the API reports them."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0

    def __post_init__(self) -> None:
        for f in fields(self):
            if getattr(self, f.name) < 0:
                raise ValueError(f"{f.name} tokens can't be negative")

    def __add__(self, other: Usage) -> Usage:
        return Usage(*(getattr(self, f.name) + getattr(other, f.name) for f in fields(self)))

    @classmethod
    def from_api(cls, usage: Any) -> Usage:
        """Map a Messages API `usage` onto Usage, the same way for every agent.

        It may be the SDK's object or, from the Agent SDK's stream, a plain dict.
        The cache fields are nullable. When the 5m/1h split of cache writes is
        missing, the writes are charged at the dearer 1h price.
        """

        def get(source: Any, name: str) -> Any:
            return source.get(name) if isinstance(source, Mapping) else getattr(source, name, None)

        split = get(usage, "cache_creation")
        if split is not None:
            write_5m = get(split, "ephemeral_5m_input_tokens") or 0
            write_1h = get(split, "ephemeral_1h_input_tokens") or 0
        else:
            write_5m, write_1h = 0, get(usage, "cache_creation_input_tokens") or 0
        return cls(
            input=get(usage, "input_tokens") or 0,
            output=get(usage, "output_tokens") or 0,
            cache_read=get(usage, "cache_read_input_tokens") or 0,
            cache_write_5m=write_5m,
            cache_write_1h=write_1h,
        )

    @property
    def prompt_tokens(self) -> int:
        return self.input + self.cache_read + self.cache_write_5m + self.cache_write_1h


def price_for(model: str, prompt_tokens: int) -> Price:
    if model not in PRICES:
        raise UnknownModel(model)
    if model in LONG_PROMPT_PRICES:
        threshold, long_price = LONG_PROMPT_PRICES[model]
        if prompt_tokens > threshold:
            return long_price
    return PRICES[model]


def cost_usd(model: str, usage: Usage) -> Decimal:
    """What one call actually cost."""
    p = price_for(model, usage.prompt_tokens)
    total = (
        usage.input * p.input
        + usage.cache_write_5m * p.cache_write_5m
        + usage.cache_write_1h * p.cache_write_1h
        + usage.cache_read * p.cache_read
        + usage.output * p.output
    )
    return total / MTOK


def worst_case_usd(model: str, input_tokens: int, max_tokens: int) -> Decimal:
    """The most a call with this many prompt tokens and this max_tokens can cost.

    Every prompt token is charged at the dearest input-side price (a 1h cache
    write), and the output at max_tokens. A model with long-prompt prices is
    always charged at them: input_tokens is an estimate, and a prompt counted
    just under the threshold can be billed just over it.
    """
    if input_tokens < 0 or max_tokens < 0:
        raise ValueError("token counts can't be negative")
    p = LONG_PROMPT_PRICES[model][1] if model in LONG_PROMPT_PRICES else price_for(model, input_tokens)
    return (input_tokens * p.max_input_side + max_tokens * p.output) / MTOK
