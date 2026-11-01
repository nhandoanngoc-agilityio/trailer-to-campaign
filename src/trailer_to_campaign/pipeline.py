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

import functools
import json
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

import anyio

from trailer_to_campaign.agents.key_moments import KeyMoment
from trailer_to_campaign.agents.loop import AgentFailed
from trailer_to_campaign.agents.review import CheckedPost, FilmBudgetReachedMidReview, for_the_marketer
from trailer_to_campaign.agents.social_posts import SocialPost
from trailer_to_campaign.approval import (
    ApprovalAborted,
    Approver,
    KeyMomentDecision,
    PostDecision,
    Recheck,
    key_moment_text,
    measured_change,
)
from trailer_to_campaign.budget import FilmBudgetReached, FilmMeter
from trailer_to_campaign.config import Settings
from trailer_to_campaign.run_log import EXIT_AGENT_FAILED, EXIT_FILM_BUDGET_REACHED, RunLog

# A Light Edit that changed more of the words than this is listed for a second look (never overridden).
LIGHT_EDIT_FLAG = 0.30

# Code traces the Copywriter's claims because it lists them; a person's edit lists none.
UNTRACED_EDIT = (
    "Facts you added aren't traced: code checked the quotes, title, limits and brand rules, "
    "and the Checker judged spoilers and invented details. Check new facts yourself."
)


@dataclass(frozen=True)
class Stages:
    watch: Callable[[FilmMeter], Awaitable[list[KeyMoment]]]
    write_and_check: Callable[[list[KeyMoment], FilmMeter], Awaitable[list[CheckedPost]]]
    recheck: Callable[[SocialPost, list[KeyMoment], FilmMeter], Awaitable[Recheck]]


def real_stages(film: str, source: str, settings: Settings, make_client: Callable[[], Any]) -> Stages:
    """The real agents. Each stage runs in its own event loop (anyio.run), and an async client is bound
    to the loop it first ran in, so every stage makes its own client and closes it."""
    from trailer_to_campaign.agents import checker, review
    from trailer_to_campaign.agents.social_posts import NotTraced, trace
    from trailer_to_campaign.catalog import load_film

    @functools.cache
    def catalog() -> dict[str, Any]:  # read once per Run: it can't change mid-Run
        return asdict(load_film(settings.films_dir, film).catalog)

    watch = watch_stage(film, source, settings, make_client)

    async def write_and_check(key_moments: list[KeyMoment], meter: FilmMeter) -> list[CheckedPost]:
        async with stage_client(make_client) as client:
            agents = review.agents_for(film, settings, client, meter)
            return await review.write_and_check(key_moments, agents, meter)

    async def recheck(post: SocialPost, key_moments: list[KeyMoment], meter: FilmMeter) -> Recheck:
        """A marketer's edit, traced and checked once: shown to them, never a gate on their decision."""
        try:
            trace([asdict(post)], key_moments, catalog(), required={(post.platform, post.language)})
            problems = []
        except NotTraced as err:
            problems = err.problems
        try:
            async with stage_client(make_client) as client:
                servers = checker.checker_servers(settings)
                [verdict] = await checker.check_posts(film, [post], servers=servers, client=client, meter=meter)
        except Exception as err:  # what tracing found still counts, and the Marketer still decides
            return Recheck(problems, None, note=_not_rechecked(err))
        return Recheck(problems, verdict, note=UNTRACED_EDIT)

    return Stages(watch=watch, write_and_check=write_and_check, recheck=recheck)


def watch_stage(
    film: str, source: str, settings: Settings, make_client: Callable[[], Any]
) -> Callable[[FilmMeter], Awaitable[list[KeyMoment]]]:
    """The Watcher stage, on the harness settings.watcher names: v0's own loop, or the Agent SDK (T11)."""
    from trailer_to_campaign.agents import watcher

    async def watch(meter: FilmMeter) -> list[KeyMoment]:
        if settings.watcher == "sdk":
            from trailer_to_campaign.agents import watcher_sdk

            servers = watcher.watcher_servers(settings)
            return await watcher_sdk.watch_trailer_sdk(
                film, source, servers=servers, meter=meter, api_key=settings.require_api_key()
            )
        async with stage_client(make_client) as client:
            servers = watcher.watcher_servers(settings)
            return await watcher.watch_trailer(film, source, servers=servers, client=client, meter=meter)

    return watch


@asynccontextmanager
async def stage_client(make_client: Callable[[], Any]) -> AsyncIterator[Any]:
    """A client for one stage, in that stage's event loop, closed when the stage ends."""
    client = make_client()
    try:
        yield client
    finally:
        await client.close()


def run_film(
    film: str, budget: Decimal, runs_dir: Path, stages: Stages, approver: Approver, out: TextIO = sys.stdout
) -> int:
    run_log = RunLog.create(runs_dir, film)
    meter = FilmMeter(budget, run_log)
    # The Run by its place under runs/, never a path on this machine: in Slack the whole channel reads it.
    where = Path(runs_dir.name) / run_log.path.parent.relative_to(runs_dir)
    run = _Run(film, run_log, meter, out, where)
    print(f"Run for {film} with a Film Budget of ${budget}; its Run Log is in {where}/", file=out)
    try:
        run.go(stages, approver)
    except FilmBudgetReached as err:
        if isinstance(err, FilmBudgetReachedMidReview):
            run.save("social_posts.json", [asdict(c) for c in err.checked])
        return run.end("film_budget_reached", EXIT_FILM_BUDGET_REACHED, str(err))
    except (AgentFailed, ApprovalAborted) as err:
        return run.end("error", EXIT_AGENT_FAILED, str(err))
    except BaseException:
        run.save_campaign("error")  # what the marketer decided survives Ctrl-C or a crash
        run_log.finish(meter.spent, "error")  # and the log still has the total
        raise
    return run.end("completed", 0)


class _Run:
    def __init__(self, film: str, run_log: RunLog, meter: FilmMeter, out: TextIO, where: Path) -> None:
        self.film, self.run_log, self.meter, self.out, self.where = film, run_log, meter, out, where
        self.decisions: dict[str, list[bool]] = {"key_moments": [], "social_posts": []}  # approved light or unedited?
        self.km_record: list[dict[str, Any]] = []  # what the marketer decided, kept however the Run ends
        self.posts_record: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []  # each edit's mark and measured change, for the summary

    def say(self, text: str) -> None:
        print(text, file=self.out, flush=True)

    def save(self, name: str, data: Any) -> None:
        path = self.run_log.path.parent / name
        text = json.dumps(data, ensure_ascii=False, indent=2)
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as err:  # paid for and decided: print it rather than lose it, and never to self.out:
            print(text, file=sys.stderr)  # in Slack that's the channel, and reasons can describe a spoiler (ADR 0007)
            print(f"could not save {name} ({type(err).__name__}); printed above", file=sys.stderr)

    def go(self, stages: Stages, approver: Approver) -> None:
        self.say("\nThe Watcher is studying the trailer...")
        moments = anyio.run(stages.watch, self.meter)
        self.save("key_moments.json", [asdict(m) for m in moments])

        self.say(f"\nApproval of {len(moments)} Key Moments")
        approved = []
        for i, moment in enumerate(moments, start=1):
            decision = approver.key_moment(i, len(moments), moment)
            kept = _apply(moment, decision)
            changed = decision.changed  # the measured change the Marketer was shown when marking it
            if changed is None and decision.decision == "edit" and kept:  # an approver that didn't measure it
                before, after = (key_moment_text(m.what_happens, m.mood) for m in (moment, kept))
                changed = measured_change(before, after)
            self._record(
                "key_moments",
                decision.decision,
                decision.light,
                decision.by,
                changed,
                where=f"key moment {i}",
                edited_by=decision.edited_by,
                item=i,
                **_km_fields(decision),
            )
            self.km_record.append(
                {"key_moment": asdict(kept or moment), "decision": decision.decision, "light": decision.light}
            )
            if kept:
                approved.append(kept)

        if approved:
            self.say(f"\nThe Copywriter and the Checker are working from {len(approved)} Key Moments...")
            checked = anyio.run(stages.write_and_check, approved, self.meter)
            self.save("social_posts.json", [asdict(c) for c in checked])
            self.say(f"\nApproval of {len(checked)} Social Posts")
            for i, c in enumerate(checked, start=1):
                rechecks: dict[str, Recheck] = {}
                decision = approver.social_post(i, len(checked), c, self._rechecker(stages, c, approved, rechecks))
                outcome = _edit_outcome(decision, rechecks)
                changed = decision.changed  # as shown to the Marketer; measured here only if the approver didn't
                if changed is None and decision.decision == "edit" and decision.text is not None:
                    changed = measured_change(c.post.text, decision.text)
                self._record(
                    "social_posts",
                    decision.decision,
                    decision.light,
                    decision.by,
                    changed,
                    where=f"{c.post.platform} {c.post.language}",
                    edited_by=decision.edited_by,
                    platform=c.post.platform,
                    language=c.post.language,
                    status=c.status,
                    text=decision.text,
                    **outcome,
                )
                self.posts_record.append({**asdict(c), **_post_fields(c, decision), **outcome})
        else:
            self.say("\nNo Key Moment was approved, so no Social Posts are written.")

    def save_campaign(self, stop: str) -> None:
        if self.km_record:  # nothing was decided before the Key Moments
            campaign = {"film": self.film, "stop_reason": stop, "key_moments": self.km_record}
            self.save("campaign.json", {**campaign, "social_posts": self.posts_record})

    def _rechecker(
        self, stages: Stages, c: CheckedPost, approved: list[KeyMoment], rechecks: dict[str, Recheck]
    ) -> Callable[[str], Recheck]:
        """Re-check a marketer's edit, and remember the result by its text, for the record of what they decide."""

        def recheck(text: str) -> Recheck:
            try:
                result = anyio.run(stages.recheck, replace(c.post, text=text), approved, self.meter)
            except Exception as err:  # a re-check informs the marketer; it never ends their Run
                result = Recheck([], None, note=_not_rechecked(err))
            rechecks[text] = result
            return result

        return recheck

    def _record(
        self,
        stage: str,
        decision: str,
        light: bool | None,
        by: str | None,
        changed: float | None,
        where: str,
        edited_by: str | None = None,
        **fields: Any,
    ) -> None:
        if by is not None:  # the Marketer who decided, when the surface knows (Slack does; a terminal doesn't)
            fields["by"] = by
        if edited_by is not None:  # the Marketer who wrote the edit (in Slack, maybe not the one who approved it)
            fields["edited_by"] = edited_by
        if changed is not None:  # an edit: how much it changed, beside the Marketer's Light/heavy mark
            fields["changed"] = changed
            self.edits.append({"where": where, "light": light is True, "changed": changed})
        self.meter.event("approval", stage=stage, decision=decision, light=light, **fields)
        self.decisions[stage].append(decision == "approve" or (decision == "edit" and light is True))

    def end(self, stop: str, code: int, message: str | None = None) -> int:
        if message:
            self.say(message)
        self.save_campaign(stop)
        rates = {stage: _rate(d) for stage, d in self.decisions.items()}
        rates["overall"] = _rate([x for d in self.decisions.values() for x in d])
        over_a_flag = [_name(p) for p in self.posts_record if _approved_over_a_flag(p)]
        not_rechecked = [_name(p) for p in self.posts_record if p.get("edit_check") == "not_rechecked"]
        light = [e["changed"] for e in self.edits if e["light"]]
        heavy = [e["changed"] for e in self.edits if not e["light"]]
        outliers = [
            {"where": e["where"], "changed": e["changed"]}
            for e in self.edits
            if e["light"] and e["changed"] > LIGHT_EDIT_FLAG
        ]
        if any(self.decisions.values()):
            self.meter.event(
                "approval_rate",
                **rates,
                light_edit_change=_mean(light),
                heavy_edit_change=_mean(heavy),
                light_edit_flag=LIGHT_EDIT_FLAG,
                light_edits_over_flag=outliers,
                approved_over_a_flag=over_a_flag,
                approved_not_rechecked=not_rechecked,
            )
        self.run_log.finish(self.meter.spent, stop)
        self.say(f"\nRun {stop}: spent ${self.meter.spent:.4f} of ${self.meter.limit}.")
        if any(self.decisions.values()):
            self.say(
                f"Approval Rate (approved unedited or with a Light Edit): Key Moments {rates['key_moments']}, "
                f"Social Posts {rates['social_posts']}, overall {rates['overall']}."
            )
        if self.edits:
            self.say(
                "Measured change (share of words an edit changed): "
                f"Light Edits {_shown(light)}, heavy edits {_shown(heavy)}."
            )
        if outliers:
            listed = ", ".join(f"{o['where']} ({o['changed']:.0%})" for o in outliers)
            self.say(
                f"Light Edits that changed over {LIGHT_EDIT_FLAG:.0%} of the words, worth a second look: {listed}."
            )
        if over_a_flag:  # names only: in Slack the channel reads this, and reasons can name a spoiler
            self.say(f"Social Posts approved over a flag: {', '.join(over_a_flag)}.")
        if not_rechecked:
            self.say(f"Social Posts approved with no re-check: {', '.join(not_rechecked)}.")
        self.say(f"Files are next to the Run Log, in {self.where}/")
        return code


def _mean(changes: list[float]) -> dict[str, Any]:
    return {"count": len(changes), "mean": round(sum(changes) / len(changes), 3) if changes else None}


def _shown(changes: list[float]) -> str:
    return f"{sum(changes) / len(changes):.0%} on average ({len(changes)})" if changes else "none"


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


def _not_rechecked(err: Exception) -> str:
    if isinstance(err, FilmBudgetReached):
        return "not re-checked: the Film Budget was reached"
    return f"not re-checked: {err}" if isinstance(err, AgentFailed) else f"not re-checked: {type(err).__name__}: {err}"


def _edit_outcome(decision: PostDecision, rechecks: dict[str, Recheck]) -> dict[str, Any]:
    """How the re-check of an approved edit's final text ended: an objection first, then a gap.

    passed means the Checker passed it and code's checks found nothing. It doesn't mean a fact the
    Marketer added was traced: the Marketer is told so (UNTRACED_EDIT), and no code can check that.
    """
    if decision.decision != "edit":
        return {}
    result = rechecks.get(decision.text or "")
    if result is None:
        return {"edit_check": "not_rechecked", "edit_problems": ["no re-check of the final text"]}
    found = [*result.problems, *(for_the_marketer(result.verdict) if result.verdict else [])]
    if result.verdict is None:
        why = [*found, result.note or "not re-checked"]
        return {"edit_check": "flagged" if found else "not_rechecked", "edit_problems": why}
    flagged = found or not result.verdict.passed
    return {"edit_check": "flagged" if flagged else "passed", "edit_problems": found}


def _name(record: dict[str, Any]) -> str:
    return f"{record['post']['platform']} {record['post']['language']}"


def _approved_over_a_flag(record: dict[str, Any]) -> bool:
    """An edit its re-check flagged, or a Flagged post approved as it stood. A rejected one doesn't go out."""
    return record.get("edit_check") == "flagged" or (record["decision"] == "approve" and record["status"] == "flagged")


def _post_fields(c: CheckedPost, decision: PostDecision) -> dict[str, Any]:
    final = decision.text if decision.decision == "edit" else c.post.text if decision.decision == "approve" else None
    return {"decision": decision.decision, "light": decision.light, "final_text": final}


def _rate(approved: list[bool]) -> str:
    return f"{sum(approved)}/{len(approved)}"
