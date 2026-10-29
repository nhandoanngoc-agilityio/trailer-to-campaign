"""One Run for one Film, in the fixed order of ADR 0004:

    Watcher -> Approval of Key Moments -> Copywriter + Checker -> Approval of Social Posts

One Film Budget and one Run Log cover every stage, so the log shows the Run's spend,
its retries and every Approval decision. The Approval of Key Moments is a pause
inside the Run, however long the marketer takes. Only the approver decides a
draft: a marketer who leaves ends the Run in an error, never in an approval.

The agent stages are injected (Stages), so the order and the Approval rules are
tested without a model; real_stages() wires the real agents.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

import anyio

from trailer_to_campaign.agents.key_moments import KeyMoment
from trailer_to_campaign.agents.loop import AgentFailed
from trailer_to_campaign.agents.review import CheckedPost, FilmBudgetReachedMidReview
from trailer_to_campaign.agents.social_posts import SocialPost
from trailer_to_campaign.approval import ApprovalAborted, Approver, KeyMomentDecision, PostDecision, Recheck
from trailer_to_campaign.budget import FilmBudgetReached, FilmMeter
from trailer_to_campaign.config import Settings
from trailer_to_campaign.run_log import RunLog

EXIT_AGENT_FAILED = 1
EXIT_FILM_BUDGET_REACHED = 3


@dataclass(frozen=True)
class Stages:
    watch: Callable[[FilmMeter], Awaitable[list[KeyMoment]]]
    write_and_check: Callable[[list[KeyMoment], FilmMeter], Awaitable[list[CheckedPost]]]
    recheck: Callable[[SocialPost, list[KeyMoment], FilmMeter], Awaitable[Recheck]]


def real_stages(film: str, source: str, settings: Settings, client: Any) -> Stages:
    from trailer_to_campaign.agents import checker, review, watcher
    from trailer_to_campaign.agents.social_posts import NotTraced, trace
    from trailer_to_campaign.catalog import load_film

    async def watch(meter: FilmMeter) -> list[KeyMoment]:
        servers = watcher.watcher_servers(settings)
        return await watcher.watch_trailer(film, source, servers=servers, client=client, meter=meter)

    async def write_and_check(key_moments: list[KeyMoment], meter: FilmMeter) -> list[CheckedPost]:
        return await review.write_and_check(key_moments, review.agents_for(film, settings, client, meter), meter)

    async def recheck(post: SocialPost, key_moments: list[KeyMoment], meter: FilmMeter) -> Recheck:
        """A marketer's edit, traced and checked once: shown to them, never a gate on their decision."""
        catalog = asdict(load_film(settings.films_dir, film).catalog)
        try:
            trace([asdict(post)], key_moments, catalog, required={(post.platform, post.language)})
            problems = []
        except NotTraced as err:
            problems = err.problems
        servers = checker.checker_servers(settings)
        [verdict] = await checker.check_posts(film, [post], servers=servers, client=client, meter=meter)
        return Recheck(problems, verdict)

    return Stages(watch=watch, write_and_check=write_and_check, recheck=recheck)


def run_film(
    film: str, budget: Decimal, runs_dir: Path, stages: Stages, approver: Approver, out: TextIO = sys.stdout
) -> int:
    run_log = RunLog.create(runs_dir, film)
    meter = FilmMeter(budget, run_log)
    run = _Run(film, run_log, meter, out)
    print(f"Run for {film} with a Film Budget of ${budget}; log at {run_log.path}", file=out)
    try:
        run.go(stages, approver)
    except FilmBudgetReached as err:
        if isinstance(err, FilmBudgetReachedMidReview):
            run.save("social_posts.json", [asdict(c) for c in err.checked])
        return run.end("film_budget_reached", EXIT_FILM_BUDGET_REACHED, str(err))
    except (AgentFailed, ApprovalAborted) as err:
        return run.end("error", EXIT_AGENT_FAILED, str(err))
    except BaseException:
        run_log.finish(meter.spent, "error")  # Ctrl-C or a crash still leaves the total
        raise
    return run.end("completed", 0)


class _Run:
    def __init__(self, film: str, run_log: RunLog, meter: FilmMeter, out: TextIO) -> None:
        self.film, self.run_log, self.meter, self.out = film, run_log, meter, out
        self.decisions: dict[str, list[bool]] = {"key_moments": [], "social_posts": []}  # approved light or unedited?

    def say(self, text: str) -> None:
        print(text, file=self.out, flush=True)

    def save(self, name: str, data: Any) -> None:
        path = self.run_log.path.parent / name
        text = json.dumps(data, ensure_ascii=False, indent=2)
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as err:  # paid for and decided: print it rather than lose it
            self.say(text)
            print(f"could not save {name} ({type(err).__name__}); printed above", file=sys.stderr)

    def go(self, stages: Stages, approver: Approver) -> None:
        self.say("\nThe Watcher is studying the trailer...")
        moments = anyio.run(stages.watch, self.meter)
        self.save("key_moments.json", [asdict(m) for m in moments])

        self.say(f"\nApproval of {len(moments)} Key Moments")
        approved, km_record = [], []
        for i, moment in enumerate(moments, start=1):
            decision = approver.key_moment(i, len(moments), moment)
            self._record("key_moments", decision.decision, decision.light, item=i, **_km_fields(decision))
            kept = _apply(moment, decision)
            km_record.append(
                {"key_moment": asdict(kept or moment), "decision": decision.decision, "light": decision.light}
            )
            if kept:
                approved.append(kept)

        posts_record: list[dict[str, Any]] = []
        if approved:
            self.say(f"\nThe Copywriter and the Checker are working from {len(approved)} Key Moments...")
            checked = anyio.run(stages.write_and_check, approved, self.meter)
            self.save("social_posts.json", [asdict(c) for c in checked])
            self.say(f"\nApproval of {len(checked)} Social Posts")
            for i, c in enumerate(checked, start=1):
                decision = approver.social_post(i, len(checked), c, self._rechecker(stages, c, approved))
                self._record(
                    "social_posts",
                    decision.decision,
                    decision.light,
                    platform=c.post.platform,
                    language=c.post.language,
                    status=c.status,
                    text=decision.text,
                )
                posts_record.append({**asdict(c), **_post_fields(c, decision)})
        else:
            self.say("\nNo Key Moment was approved, so no Social Posts are written.")
        self.save("campaign.json", {"film": self.film, "key_moments": km_record, "social_posts": posts_record})

    def _rechecker(self, stages: Stages, c: CheckedPost, approved: list[KeyMoment]) -> Callable[[str], Recheck]:
        def recheck(text: str) -> Recheck:
            try:
                return anyio.run(stages.recheck, replace(c.post, text=text), approved, self.meter)
            except FilmBudgetReached:
                return Recheck([], None, note="not re-checked: the Film Budget was reached")
            except AgentFailed as err:
                return Recheck([], None, note=f"not re-checked: {err}")

        return recheck

    def _record(self, stage: str, decision: str, light: bool | None, **fields: Any) -> None:
        self.meter.event("approval", stage=stage, decision=decision, light=light, **fields)
        self.decisions[stage].append(decision == "approve" or (decision == "edit" and light is True))

    def end(self, stop: str, code: int, message: str | None = None) -> int:
        if message:
            self.say(message)
        rates = {stage: _rate(d) for stage, d in self.decisions.items()}
        rates["overall"] = _rate([x for d in self.decisions.values() for x in d])
        if any(self.decisions.values()):
            self.meter.event("approval_rate", **rates)
        self.run_log.finish(self.meter.spent, stop)
        self.say(f"\nRun {stop}: spent ${self.meter.spent:.4f} of ${self.meter.limit}.")
        if any(self.decisions.values()):
            self.say(
                f"Approval Rate (approved unedited or with a Light Edit): Key Moments {rates['key_moments']}, "
                f"Social Posts {rates['social_posts']}, overall {rates['overall']}."
            )
        self.say(f"Files are next to the Run Log: {self.run_log.path.parent}")
        return code


def _km_fields(decision: KeyMomentDecision) -> dict[str, Any]:
    return {"what_happens": decision.what_happens, "mood": decision.mood} if decision.decision == "edit" else {}


def _apply(moment: KeyMoment, decision: KeyMomentDecision) -> KeyMoment | None:
    """The Key Moment as approved (with the marketer's edit), or None if rejected."""
    if decision.decision == "approve":
        return moment
    if decision.decision == "edit":
        return replace(
            moment, what_happens=decision.what_happens or moment.what_happens, mood=decision.mood or moment.mood
        )
    return None


def _post_fields(c: CheckedPost, decision: PostDecision) -> dict[str, Any]:
    final = decision.text if decision.decision == "edit" else c.post.text if decision.decision == "approve" else None
    return {"decision": decision.decision, "light": decision.light, "final_text": final}


def _rate(approved: list[bool]) -> str:
    return f"{sum(approved)}/{len(approved)}"
