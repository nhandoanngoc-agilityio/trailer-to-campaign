"""Tests for the model price table that the Film Budget meter charges from."""

from __future__ import annotations

from decimal import Decimal

import pytest

from trailer_to_campaign.pricing import UnknownModel, Usage, cost_usd, worst_case_usd

MTOK = 1_000_000


def test_opus_input_and_output() -> None:
    assert cost_usd("claude-opus-5-5", Usage(input=MTOK, output=MTOK)) == Decimal("24")


def test_cache_tokens_use_their_own_prices() -> None:
    usage = Usage(cache_read=MTOK, cache_write_5m=MTOK, cache_write_1h=MTOK)
    assert cost_usd("claude-sonnet-5-5", usage) == Decimal("0.10") + Decimal("2.50") + Decimal("4")


def test_small_counts_are_exact() -> None:
    assert cost_usd("claude-fable-5-1", Usage(input=1, output=1)) == Decimal("0.00006")


def test_haiku_base_tier_up_to_100k_prompt_tokens() -> None:
    usage = Usage(input=60_000, cache_read=40_000, output=MTOK)
    assert cost_usd("claude-haiku-5-5", usage) == Decimal("0.006") + Decimal("0.0004") + Decimal("0.50")


def test_haiku_long_tier_over_100k_prompt_tokens() -> None:
    usage = Usage(input=60_000, cache_write_5m=40_001, output=MTOK)
    expected = Decimal("0.03") + Decimal("40001") * Decimal("0.625") / MTOK + Decimal("2.50")
    assert cost_usd("claude-haiku-5-5", usage) == expected


def test_unknown_model_is_refused() -> None:
    with pytest.raises(UnknownModel, match="claude-opus-9"):
        cost_usd("claude-opus-9", Usage(input=1))
    with pytest.raises(UnknownModel):
        worst_case_usd("gpt-5", input_tokens=1, max_tokens=1)


@pytest.mark.parametrize("model", ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"])
@pytest.mark.parametrize("prompt_tokens", [1_000, 100_000, 100_001, 500_000])
@pytest.mark.parametrize("field", ["input", "cache_read", "cache_write_5m", "cache_write_1h"])
def test_worst_case_covers_any_mix_of_the_same_totals(model: str, prompt_tokens: int, field: str) -> None:
    max_tokens = 16_000
    actual = cost_usd(model, Usage(output=max_tokens, **{field: prompt_tokens}))
    assert worst_case_usd(model, input_tokens=prompt_tokens, max_tokens=max_tokens) >= actual


def test_negative_tokens_are_rejected() -> None:
    with pytest.raises(ValueError):
        Usage(input=-1)
    with pytest.raises(ValueError):
        worst_case_usd("claude-opus-5-5", input_tokens=-1, max_tokens=1)
