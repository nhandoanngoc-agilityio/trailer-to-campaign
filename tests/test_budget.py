"""Tests for the Film Budget meter: the hard stop on model spend (ADR 0005)."""

from __future__ import annotations

import json
import random
from decimal import Decimal
from pathlib import Path

import pytest

from trailer_to_campaign.budget import FilmBudgetReached, FilmMeter
from trailer_to_campaign.pricing import UnknownModel, Usage, cost_usd, worst_case_usd
from trailer_to_campaign.run_log import RunLog

OPUS = "claude-opus-5-5"


@pytest.fixture
def log(tmp_path: Path) -> RunLog:
    return RunLog.create(tmp_path, "sample")


def events(log: RunLog, kind: str) -> list[dict]:
    lines = [json.loads(line) for line in log.path.read_text().splitlines()]
    return [line for line in lines if line["event"] == kind]


def test_records_actual_cost(log: RunLog) -> None:
    meter = FilmMeter(Decimal("2"), log)
    usage = Usage(input=10_000, output=2_000)
    with meter.call(OPUS, input_tokens=10_000, max_tokens=16_000, agent="watcher") as call:
        call.record(usage)
    assert meter.spent == cost_usd(OPUS, usage)
    [line] = events(log, "model_call")
    assert line["model"] == OPUS
    assert line["agent"] == "watcher"
    assert line["input_tokens"] == 10_000
    assert line["output_tokens"] == 2_000
    assert line["charged"] == "actual"
    assert Decimal(line["cost_usd"]) == cost_usd(OPUS, usage)
    assert Decimal(line["spent_usd"]) == meter.spent


def test_refuses_a_call_that_could_exceed_the_budget_before_it_runs(log: RunLog) -> None:
    limit = worst_case_usd(OPUS, input_tokens=1_000, max_tokens=1_000) - Decimal("0.000001")
    meter = FilmMeter(limit, log)
    ran = False
    with pytest.raises(FilmBudgetReached):
        with meter.call(OPUS, input_tokens=1_000, max_tokens=1_000):
            ran = True
    assert not ran
    assert meter.spent == 0
    assert events(log, "model_call") == []
    assert len(events(log, "film_budget_reached")) == 1


def test_a_call_that_fits_exactly_is_allowed(log: RunLog) -> None:
    meter = FilmMeter(worst_case_usd(OPUS, input_tokens=1_000, max_tokens=1_000), log)
    with meter.call(OPUS, input_tokens=1_000, max_tokens=1_000) as call:
        call.record(Usage(input=1_000, output=1_000))


def test_once_spend_reaches_the_limit_the_next_call_is_refused(log: RunLog) -> None:
    usage = Usage(cache_write_1h=1_000, output=1_000)  # costs exactly the worst case
    meter = FilmMeter(worst_case_usd(OPUS, input_tokens=1_000, max_tokens=1_000), log)
    with meter.call(OPUS, input_tokens=1_000, max_tokens=1_000) as call:
        call.record(usage)
    assert meter.spent == meter.limit
    with pytest.raises(FilmBudgetReached):
        with meter.call(OPUS, input_tokens=1, max_tokens=1):
            pass


def test_spend_never_exceeds_the_limit(log: RunLog) -> None:
    rng = random.Random(1)
    meter = FilmMeter(Decimal("0.50"), log)
    stopped = False
    for _ in range(500):
        input_tokens, max_tokens = rng.randint(0, 20_000), rng.randint(1, 8_000)
        try:
            with meter.call(OPUS, input_tokens=input_tokens, max_tokens=max_tokens) as call:
                split = rng.randint(0, input_tokens)
                call.record(Usage(input=split, cache_write_1h=input_tokens - split, output=rng.randint(0, max_tokens)))
        except FilmBudgetReached:
            stopped = True
        assert meter.spent <= meter.limit
    assert stopped


def test_an_error_after_the_call_charges_the_worst_case(log: RunLog) -> None:
    meter = FilmMeter(Decimal("2"), log)
    with pytest.raises(TimeoutError):
        with meter.call(OPUS, input_tokens=1_000, max_tokens=1_000):
            raise TimeoutError("the API call may still have been billed")
    assert meter.spent == worst_case_usd(OPUS, input_tokens=1_000, max_tokens=1_000)
    [line] = events(log, "model_call")
    assert line["charged"] == "worst_case"
    assert line["error"] == "TimeoutError"


def test_leaving_without_recording_charges_the_worst_case(log: RunLog) -> None:
    meter = FilmMeter(Decimal("2"), log)
    with meter.call(OPUS, input_tokens=1_000, max_tokens=1_000):
        pass
    assert meter.spent == worst_case_usd(OPUS, input_tokens=1_000, max_tokens=1_000)


def test_open_calls_count_against_the_budget(log: RunLog) -> None:
    worst = worst_case_usd(OPUS, input_tokens=1_000, max_tokens=1_000)
    meter = FilmMeter(worst * 3 / 2, log)
    with meter.call(OPUS, input_tokens=1_000, max_tokens=1_000) as outer:
        with pytest.raises(FilmBudgetReached):
            with meter.call(OPUS, input_tokens=1_000, max_tokens=1_000):
                pass
        outer.record(Usage(input=1_000, output=1_000))


def test_usage_can_only_be_recorded_once(log: RunLog) -> None:
    meter = FilmMeter(Decimal("2"), log)
    with pytest.raises(RuntimeError):
        with meter.call(OPUS, input_tokens=10, max_tokens=10) as call:
            call.record(Usage(input=10))
            call.record(Usage(input=10))


def test_unknown_model_is_refused_without_charge(log: RunLog) -> None:
    meter = FilmMeter(Decimal("2"), log)
    with pytest.raises(UnknownModel):
        with meter.call("claude-opus-9", input_tokens=10, max_tokens=10):
            pass
    assert meter.spent == 0


def test_message_says_film_budget_reached_with_amounts(log: RunLog) -> None:
    meter = FilmMeter(Decimal("0.001"), log)
    with pytest.raises(FilmBudgetReached) as err:
        with meter.call(OPUS, input_tokens=1_000, max_tokens=1_000):
            pass
    assert str(err.value).startswith("Film Budget reached: spent $0.000000 of $0.001")


@pytest.mark.parametrize("limit", ["0", "-1", "NaN", "Infinity"])
def test_limit_must_be_positive_and_finite(log: RunLog, limit: str) -> None:
    with pytest.raises(ValueError):
        FilmMeter(Decimal(limit), log)


def test_recorded_usage_is_charged_even_if_the_block_raises_later(log: RunLog) -> None:
    meter = FilmMeter(Decimal("2"), log)
    usage = Usage(input=1_000, output=100)
    with pytest.raises(ValueError):
        with meter.call(OPUS, input_tokens=1_000, max_tokens=1_000) as call:
            call.record(usage)
            raise ValueError("parsing the response failed")
    assert meter.spent == cost_usd(OPUS, usage)
    [line] = events(log, "model_call")
    assert line["charged"] == "actual"
    assert line["error"] == "ValueError"


def test_haiku_reservation_covers_a_prompt_that_crosses_the_long_tier(log: RunLog) -> None:
    # count_tokens is an estimate: a 99,990-token estimate can bill as a long prompt.
    meter = FilmMeter(Decimal("2"), log)
    with meter.call("claude-haiku-5-5", input_tokens=99_990, max_tokens=1_000) as call:
        call.record(Usage(input=99_990, cache_write_1h=20, output=1_000))  # 100,010 tokens
    [line] = events(log, "model_call")
    assert Decimal(line["cost_usd"]) <= Decimal(line["reserved_usd"])
    assert events(log, "reservation_exceeded") == []


def test_a_call_that_costs_more_than_reserved_is_logged(log: RunLog) -> None:
    meter = FilmMeter(Decimal("2"), log)
    with meter.call(OPUS, input_tokens=100, max_tokens=100) as call:
        call.record(Usage(input=50_000, output=100))
    assert meter.spent == cost_usd(OPUS, Usage(input=50_000, output=100))
    [line] = events(log, "reservation_exceeded")
    assert line["input_tokens_reserved"] == 100
    assert line["prompt_tokens_billed"] == 50_000


def test_a_metered_call_cannot_be_entered_twice(log: RunLog) -> None:
    meter = FilmMeter(Decimal("2"), log)
    call = meter.call(OPUS, input_tokens=10, max_tokens=10)
    with call:
        call.record(Usage(input=10))
    with pytest.raises(RuntimeError):
        with call:
            pass
    assert len(events(log, "model_call")) == 1


def test_usage_cannot_be_recorded_outside_the_call(log: RunLog) -> None:
    meter = FilmMeter(Decimal("2"), log)
    call = meter.call(OPUS, input_tokens=10, max_tokens=10)
    with pytest.raises(RuntimeError):
        call.record(Usage(input=10))
    with call:
        call.record(Usage(input=10))
    with pytest.raises(RuntimeError):
        call.record(Usage(input=10))


def test_a_failed_log_write_keeps_the_paid_for_call(log: RunLog) -> None:
    meter = FilmMeter(Decimal("2"), log)
    usage = Usage(input=1_000, output=100)
    log.path = log.path.parent / "gone" / "run.jsonl"  # the log can no longer be appended
    with meter.call(OPUS, input_tokens=1_000, max_tokens=1_000) as call:
        call.record(usage)
    assert meter.spent == cost_usd(OPUS, usage)
    assert meter.run_log_failed


def test_a_failed_log_write_does_not_hide_the_original_error(log: RunLog) -> None:
    meter = FilmMeter(Decimal("2"), log)
    log.path = log.path.parent / "gone" / "run.jsonl"
    with pytest.raises(TimeoutError):
        with meter.call(OPUS, input_tokens=1_000, max_tokens=1_000):
            raise TimeoutError
    assert meter.spent == worst_case_usd(OPUS, input_tokens=1_000, max_tokens=1_000)


# T11: the SDK Watcher's spend is charged after the fact (ADR 0009), inside a cap set from what's left.


def test_remaining_is_what_is_left_after_spend_and_open_calls(log: RunLog) -> None:
    meter = FilmMeter(Decimal("2"), log)
    assert meter.remaining() == Decimal("2")
    with meter.call(OPUS, input_tokens=1_000, max_tokens=1_000) as call:
        assert meter.remaining() == Decimal("2") - call.worst
        call.record(Usage(input=1_000, output=1_000))
    assert meter.remaining() == Decimal("2") - meter.spent


def test_usage_spent_already_is_charged_at_our_prices(log: RunLog) -> None:
    meter = FilmMeter(Decimal("2"), log)
    usage = Usage(input=5_000, output=800, cache_read=20_000)
    meter.charge_after(OPUS, agent="watcher", usage=usage)
    assert meter.spent == cost_usd(OPUS, usage)
    [line] = events(log, "model_call")
    assert (line["agent"], line["charged"], Decimal(line["reserved_usd"])) == ("watcher", "after", 0)
    assert (line["input_tokens"], line["output_tokens"], line["cache_read_tokens"]) == (5_000, 800, 20_000)


def test_a_cost_without_usage_is_charged_as_given_and_labelled(log: RunLog) -> None:
    meter = FilmMeter(Decimal("2"), log)
    meter.charge_after("claude-unknown-9", agent="watcher", cost=Decimal("0.05"), charged="sdk_estimate")
    assert meter.spent == Decimal("0.05")
    [line] = events(log, "model_call")
    assert line["charged"] == "sdk_estimate" and line["model"] == "claude-unknown-9"


def test_a_charge_after_must_have_usage_or_a_cost(log: RunLog) -> None:
    meter = FilmMeter(Decimal("2"), log)
    with pytest.raises(ValueError):
        meter.charge_after(OPUS, agent="watcher")
    with pytest.raises(UnknownModel):
        meter.charge_after("claude-unknown-9", agent="watcher", usage=Usage(input=1))


def test_spend_charged_after_that_goes_past_the_limit_is_logged_and_stops_the_next_call(log: RunLog) -> None:
    meter = FilmMeter(Decimal("0.01"), log)
    meter.charge_after(OPUS, agent="watcher", usage=Usage(input=10_000))  # $0.04: already spent, so charged in full
    assert meter.spent == Decimal("0.04") and meter.remaining() == 0
    assert len(events(log, "film_budget_exceeded")) == 1
    with pytest.raises(FilmBudgetReached):
        with meter.call(OPUS, input_tokens=1, max_tokens=1):
            pass
