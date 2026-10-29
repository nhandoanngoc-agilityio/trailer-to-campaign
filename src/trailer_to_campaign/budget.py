"""The Film Budget meter: every model call goes through it (ADR 0005).

    with meter.call(model, input_tokens=n, max_tokens=m, agent="watcher") as call:
        response = client.messages.create(...)
        call.record(Usage(...))

Before the call, the meter reserves the call's worst-case cost and refuses it
with FilmBudgetReached if that could take spend past the limit. After the call
it charges the actual cost from the recorded usage. If the block exits without
recording usage, the call may still have been billed, so the meter charges the
worst case: it fails closed.

Spend never exceeds the Film Budget as long as input_tokens covers the whole
prompt and max_tokens is the one sent. A call that costs more than it reserved
is still charged in full (that money is spent) and logged as
reservation_exceeded, and the next call is refused once spend is over.

The pipeline is a fixed sequence (ADR 0004), so the meter is not thread-safe.
Open calls still count against the budget, so nested calls can't overspend.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from types import TracebackType

from trailer_to_campaign.pricing import Usage, cost_usd, worst_case_usd
from trailer_to_campaign.run_log import RunLog

log = logging.getLogger(__name__)


class FilmBudgetReached(Exception):
    def __init__(self, spent: Decimal, limit: Decimal, next_call: Decimal) -> None:
        self.spent, self.limit, self.next_call = spent, limit, next_call
        super().__init__(
            f"Film Budget reached: spent ${spent:.6f} of ${limit} (the next call could cost up to ${next_call:.6f})"
        )


class FilmMeter:
    def __init__(self, limit_usd: Decimal, run_log: RunLog) -> None:
        if not limit_usd.is_finite() or limit_usd <= 0:
            raise ValueError(f"Film Budget must be a positive amount, got {limit_usd}")
        self.limit = limit_usd
        self.run_log = run_log
        self.spent = Decimal(0)
        self._reserved = Decimal(0)

    def call(self, model: str, input_tokens: int, max_tokens: int, agent: str | None = None) -> MeteredCall:
        """Meter one model call. input_tokens is the whole prompt, as counted before sending."""
        return MeteredCall(self, model, input_tokens, max_tokens, agent)

    def _reserve(self, model: str, worst: Decimal) -> None:
        if self.spent + self._reserved + worst > self.limit:
            self.run_log.event(
                "film_budget_reached", model=model, spent_usd=self.spent, limit_usd=self.limit, next_call_usd=worst
            )
            raise FilmBudgetReached(self.spent, self.limit, worst)
        self._reserved += worst

    def _settle(self, worst: Decimal, charge: Decimal) -> None:
        self._reserved -= worst
        self.spent += charge


class MeteredCall:
    def __init__(self, meter: FilmMeter, model: str, input_tokens: int, max_tokens: int, agent: str | None) -> None:
        self.meter, self.model, self.agent = meter, model, agent
        self.input_tokens, self.max_tokens = input_tokens, max_tokens
        self.worst = worst_case_usd(model, input_tokens, max_tokens)  # UnknownModel stops it here
        self.usage: Usage | None = None
        self._state = "new"  # new -> open -> closed; a call is metered exactly once

    def record(self, usage: Usage) -> None:
        if self._state != "open":
            raise RuntimeError("usage can only be recorded inside `with meter.call(...)`")
        if self.usage is not None:
            raise RuntimeError("usage was already recorded for this call")
        self.usage = usage

    def __enter__(self) -> MeteredCall:
        if self._state != "new":
            raise RuntimeError("a metered call can only be used once; ask the meter for a new one")
        self.meter._reserve(self.model, self.worst)
        self._state = "open"
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._state = "closed"
        if self.usage is not None:
            charge, charged = cost_usd(self.model, self.usage), "actual"
        else:
            charge, charged = self.worst, "worst_case"
        self.meter._settle(self.worst, charge)
        if charge > self.worst:
            log.warning("%s call cost $%s, more than its reserved $%s", self.model, charge, self.worst)
            self.meter.run_log.event(
                "reservation_exceeded",
                model=self.model,
                agent=self.agent,
                input_tokens_reserved=self.input_tokens,
                max_tokens_reserved=self.max_tokens,
                prompt_tokens_billed=self.usage.prompt_tokens if self.usage else None,
                reserved_usd=self.worst,
                cost_usd=charge,
            )
        usage = self.usage or Usage()
        self.meter.run_log.event(
            "model_call",
            model=self.model,
            agent=self.agent,
            input_tokens=usage.input,
            output_tokens=usage.output,
            cache_read_tokens=usage.cache_read,
            cache_write_5m_tokens=usage.cache_write_5m,
            cache_write_1h_tokens=usage.cache_write_1h,
            reserved_usd=self.worst,
            cost_usd=charge,
            charged=charged,
            error=exc_type.__name__ if exc_type else None,
            spent_usd=self.meter.spent,
        )
