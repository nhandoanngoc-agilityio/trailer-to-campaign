"""Tests for the model price table that the Film Budget meter charges from."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

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


def test_usage_from_api_treats_missing_cache_fields_as_zero() -> None:
    api = SimpleNamespace(
        input_tokens=1_200, output_tokens=300, cache_read_input_tokens=None, cache_creation_input_tokens=None
    )
    assert Usage.from_api(api) == Usage(input=1_200, output=300)


def test_usage_from_api_reads_the_cache_write_split() -> None:
    api = SimpleNamespace(
        input_tokens=10,
        output_tokens=20,
        cache_read_input_tokens=30,
        cache_creation_input_tokens=70,
        cache_creation=SimpleNamespace(ephemeral_5m_input_tokens=40, ephemeral_1h_input_tokens=30),
    )
    assert Usage.from_api(api) == Usage(input=10, output=20, cache_read=30, cache_write_5m=40, cache_write_1h=30)


def test_usage_from_api_charges_an_unsplit_cache_write_at_the_1h_price() -> None:
    api = SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=50)
    assert Usage.from_api(api) == Usage(cache_write_1h=50)


def test_usage_from_api_reads_a_stream_events_dict_the_same_way() -> None:
    # The Agent SDK streams raw API events as dicts: one mapping for both harnesses (T11).
    split = {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 30,
             "cache_creation": {"ephemeral_5m_input_tokens": 40, "ephemeral_1h_input_tokens": 30}}  # fmt: skip
    assert Usage.from_api(split) == Usage(input=10, output=20, cache_read=30, cache_write_5m=40, cache_write_1h=30)
    assert Usage.from_api({"input_tokens": 5, "cache_creation_input_tokens": 50}) == Usage(5, 0, 0, 0, 50)


def test_usages_add_field_by_field() -> None:
    assert Usage(1, 2, 3, 4, 5) + Usage(10, 20, 30, 40, 50) == Usage(11, 22, 33, 44, 55)
