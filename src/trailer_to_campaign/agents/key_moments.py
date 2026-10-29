"""Key Moments, and the check that each one is grounded in what the Watcher saw and heard.

The Watcher submits Key Moments as tool input; ground() turns them into
KeyMoment records or refuses the whole set with every problem it found, so the
Watcher can fix them in one go. Grounded means:

- 5 to 10 moments, each inside the trailer, start before end
- each cites at least one frame the Watcher fetched, taken during the moment
- a quote is words heard in the transcript during the moment, never an unheard line
- characters are cast from the Catalog Metadata, never a guessed name

Every later Campaign Draft is built on these, so an invented line or name stops here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from trailer_to_campaign.catalog import CastMember
from trailer_to_campaign.footage import settings

MIN_MOMENTS = 5
MAX_MOMENTS = 10
# Frames are sampled every INTERVAL_S, so the nearest one can be up to half that from the moment.
TOLERANCE_S = settings.INTERVAL_S / 2


class NotGrounded(ValueError):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("Key Moments not grounded:\n" + "\n".join(f"- {p}" for p in problems))


@dataclass(frozen=True)
class Quote:
    text: str
    start: float  # when the transcript lines it was matched in are heard
    end: float


@dataclass(frozen=True)
class KeyMoment:
    start: float
    end: float
    what_happens: str
    mood: str
    quote: Quote | None
    characters: list[str]
    frames: list[int]


def ground(
    submitted: list[Any], footage: dict[str, Any], cast: list[CastMember], seen_frames: set[int]
) -> list[KeyMoment]:
    """Return the Key Moments in time order, or raise NotGrounded listing every problem."""
    problems: list[str] = []
    if not MIN_MOMENTS <= len(submitted) <= MAX_MOMENTS:
        problems.append(f"submit {MIN_MOMENTS} to {MAX_MOMENTS} Key Moments, not {len(submitted)}")
    names = _cast_names(cast)
    moments = []
    for i, raw in enumerate(submitted, start=1):
        where = f"Key Moment {i}"
        try:
            moment, found = _moment(raw, footage, names, seen_frames)
        except (KeyError, TypeError, ValueError, AttributeError):
            problems.append(f"{where}: needs start, end, what_happens, mood, quote, characters and frames")
            continue
        problems += [f"{where}: {p}" for p in found]
        if moment:
            moments.append(moment)
    if problems:
        raise NotGrounded(problems)
    return sorted(moments, key=lambda m: (m.start, m.end))


def _moment(
    raw: dict[str, Any], footage: dict[str, Any], names: dict[str, str], seen_frames: set[int]
) -> tuple[KeyMoment | None, list[str]]:
    start, end = float(raw["start"]), float(raw["end"])
    what, mood = str(raw["what_happens"]).strip(), str(raw["mood"]).strip()
    quote_text, characters, frames = raw["quote"], list(raw["characters"]), [int(f) for f in raw["frames"]]

    duration = float(footage["duration"])
    if not 0 <= start <= end <= duration:
        if start > end:
            return None, [f"start {start} must be before end {end}"]
        return None, [f"{start}-{end} s is outside the trailer, which runs 0-{duration} s"]

    problems = [f"{name} must be non-empty text" for name, text in (("what_happens", what), ("mood", mood)) if not text]
    problems += _frame_problems(frames, footage, start, end, seen_frames)

    quote = None
    if quote_text is not None:
        quote = _find_quote(str(quote_text).strip(), footage, start, end)
        if quote is None:
            problems.append(_quote_problem(str(quote_text), footage, start, end))

    named = []
    for character in characters:
        canonical = names.get(str(character).strip().casefold())
        if canonical is None:
            allowed = ", ".join(sorted(set(names.values())))
            problems.append(f"{character!r} is not in the Catalog Metadata cast ({allowed}); describe them unnamed")
        elif canonical not in named:
            named.append(canonical)

    if problems:
        return None, problems
    return KeyMoment(start, end, what, mood, quote, named, frames), []


def _frame_problems(
    frames: list[int], footage: dict[str, Any], start: float, end: float, seen_frames: set[int]
) -> list[str]:
    if not frames:
        return ["cite at least one frame you looked at with get_frame"]
    times = {f["index"]: float(f["t"]) for f in footage["frames"]}
    problems = []
    for index in frames:
        if index not in times:
            problems.append(f"frame {index} does not exist (frames are 0-{len(times) - 1})")
        elif index not in seen_frames:
            problems.append(f"frame {index} was not looked at; fetch it with get_frame or cite a frame you saw")
        elif not start - TOLERANCE_S <= times[index] <= end + TOLERANCE_S:
            problems.append(f"frame {index} is at {times[index]} s, outside the moment's {start}-{end} s")
    return problems


def _words(text: str) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", text.casefold()).split())


def _contains(haystack: str, needle: str) -> bool:
    return bool(needle) and f" {needle} " in f" {haystack} "


def _near(lines: Iterable[dict[str, Any]], start: float, end: float) -> list[dict[str, Any]]:
    return sorted(
        (s for s in lines if s["end"] >= start - TOLERANCE_S and s["start"] <= end + TOLERANCE_S),
        key=lambda s: s["start"],
    )


def _find_quote(text: str, footage: dict[str, Any], start: float, end: float) -> Quote | None:
    """The shortest run of consecutive transcript lines near the moment that holds the quote's words."""
    words = _words(text)
    lines = _near(footage["transcript"], start, end)
    for size in range(1, len(lines) + 1):
        for i in range(len(lines) - size + 1):
            run = lines[i : i + size]
            if _contains(" ".join(_words(s["text"]) for s in run), words):
                return Quote(text, start=float(run[0]["start"]), end=float(run[-1]["end"]))
    return None


def _quote_problem(text: str, footage: dict[str, Any], start: float, end: float) -> str:
    words = _words(text)
    if any(_contains(_words(s["text"]), words) for s in footage.get("unheard", [])):
        return f"quote {text!r} is an unheard line, not dialogue; never quote it"
    return f"quote {text!r} is not heard in the transcript during {start}-{end} s; quote the words exactly, or use null"


def _cast_names(cast: list[CastMember]) -> dict[str, str]:
    names = {}
    for member in cast:
        names[member.character.casefold()] = member.character
        if member.name:
            names[member.name.casefold()] = member.name
    return names
