"""Key Moments, and the check that each one is grounded in what the Watcher saw and heard.

The Watcher submits Key Moments as tool input; ground() turns them into
KeyMoment records or refuses the whole set with every problem it found, so the
Watcher can fix them in one go. Grounded means:

- 5 to 10 moments, each inside the trailer, start before end
- each cites at least one frame the Watcher fetched, taken during the moment
  (or as near it as the sampling allows)
- a quote is words heard in the transcript during the moment, never an unheard line,
  and is kept in the transcript's words, not the Watcher's
- characters are cast from the Catalog Metadata, never a guessed name; an actor's
  name is kept as the character they play

Every later Campaign Draft is built on these, so an invented line or name stops here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trailer_to_campaign.catalog import CastMember
from trailer_to_campaign.footage import settings

MIN_MOMENTS = 5
MAX_MOMENTS = 10
# How far a quote's line may be heard from the moment: Whisper's timestamps are approximate.
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


def load_key_moments(path: Path) -> list[KeyMoment]:
    """Read back the Key Moments `t2c watch` wrote. Errors name the file, never its local path."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        moments = [
            KeyMoment(
                start=float(m["start"]),
                end=float(m["end"]),
                what_happens=str(m["what_happens"]),
                mood=str(m["mood"]),
                quote=Quote(str(q["text"]), float(q["start"]), float(q["end"])) if (q := m["quote"]) else None,
                characters=[str(c) for c in m["characters"]],
                frames=[int(f) for f in m["frames"]],
            )
            for m in raw
        ]
    except OSError as err:
        raise ValueError(f"{path.name} could not be read ({type(err).__name__})") from err
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise ValueError(f"{path.name} is not JSON") from err
    except (KeyError, TypeError, ValueError, AttributeError) as err:
        raise ValueError(f"{path.name} is not a list of Key Moments as t2c watch writes them") from err
    if not moments:
        raise ValueError(f"{path.name} has no Key Moments")
    return moments


def ground(
    submitted: list[Any], footage: dict[str, Any], cast: list[CastMember], seen_frames: set[int]
) -> list[KeyMoment]:
    """Return the Key Moments in time order, or raise NotGrounded listing every problem."""
    problems: list[str] = []
    if not MIN_MOMENTS <= len(submitted) <= MAX_MOMENTS:
        problems.append(f"submit {MIN_MOMENTS} to {MAX_MOMENTS} Key Moments, not {len(submitted)}")
    names = _cast_names(cast)
    frames = _Frames(footage["frames"])
    moments = []
    for i, raw in enumerate(submitted, start=1):
        where = f"Key Moment {i}"
        try:
            moment, found = _moment(raw, footage, frames, names, seen_frames)
        except (KeyError, TypeError, ValueError, AttributeError):
            problems.append(f"{where}: needs start, end, what_happens, mood, quote, characters and frames")
            continue
        problems += [f"{where}: {p}" for p in found]
        if moment:
            moments.append(moment)
    if problems:
        raise NotGrounded(problems)
    return sorted(moments, key=lambda m: (m.start, m.end))


class _Frames:
    """Frame times by index, and how far from a moment its nearest frame can be.

    Frames come every INTERVAL_S plus one per scene change, but over MAX_FRAMES the
    interval frames are thinned, so the window is half the widest gap actually sampled.
    """

    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self.times = {f["index"]: float(f["t"]) for f in frames}
        ordered = sorted(self.times.values())
        widest = max((b - a for a, b in zip(ordered, ordered[1:], strict=False)), default=0.0)
        self.tolerance = max(settings.INTERVAL_S, widest) / 2


def _moment(
    raw: dict[str, Any], footage: dict[str, Any], frames_of: _Frames, names: dict[str, str], seen_frames: set[int]
) -> tuple[KeyMoment | None, list[str]]:
    start, end = float(raw["start"]), float(raw["end"])
    what, mood = str(raw["what_happens"]).strip(), str(raw["mood"]).strip()
    quote_text, characters, frames = raw["quote"], list(raw["characters"]), [int(f) for f in raw["frames"]]

    duration = float(footage["duration"])
    if not 0 <= start < end <= duration:
        if start >= end:
            return None, [f"start {start} must be before end {end}"]
        return None, [f"{start}-{end} s is outside the trailer, which runs 0-{duration} s"]

    problems = [f"{name} must be non-empty text" for name, text in (("what_happens", what), ("mood", mood)) if not text]
    problems += _frame_problems(frames, frames_of, start, end, seen_frames)

    quote = None
    if quote_text is not None:
        quote = _find_quote(str(quote_text), footage, start, end)
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
    frames: list[int], frames_of: _Frames, start: float, end: float, seen_frames: set[int]
) -> list[str]:
    if not frames:
        return ["cite at least one frame you looked at with get_frame"]
    times, near = frames_of.times, frames_of.tolerance
    problems = []
    for index in frames:
        if index not in times:
            problems.append(f"frame {index} does not exist (frames are 0-{len(times) - 1})")
        elif index not in seen_frames:
            problems.append(f"frame {index} was not looked at; fetch it with get_frame or cite a frame you saw")
        elif not start - near <= times[index] <= end + near:
            problems.append(f"frame {index} is at {times[index]} s, outside the moment's {start}-{end} s")
    return problems


WORD = re.compile(r"[^\W_]+")


def _words(text: str) -> str:
    return " ".join(w.casefold() for w in WORD.findall(text))


def _contains(haystack: str, needle: str) -> bool:
    return bool(needle) and f" {needle} " in f" {haystack} "


def _near(lines: Iterable[dict[str, Any]], start: float, end: float) -> list[dict[str, Any]]:
    return sorted(
        (s for s in lines if s["end"] >= start - TOLERANCE_S and s["start"] <= end + TOLERANCE_S),
        key=lambda s: s["start"],
    )


def _find_quote(text: str, footage: dict[str, Any], start: float, end: float) -> Quote | None:
    """The quote as heard: the shortest run of consecutive transcript lines near the moment that holds its words.

    Matching ignores case and punctuation, but the Quote keeps the transcript's own
    words, from the first matched word to the end of the last (with its punctuation).
    """
    wanted = _words(text).split()
    if not wanted:
        return None
    lines = _near(footage["transcript"], start, end)
    for size in range(1, len(lines) + 1):
        for i in range(len(lines) - size + 1):
            run = lines[i : i + size]
            heard = " ".join(s["text"].strip() for s in run)
            tokens = list(WORD.finditer(heard))
            words = [t.group().casefold() for t in tokens]
            for j in range(len(words) - len(wanted) + 1):
                if words[j : j + len(wanted)] == wanted:
                    first, last = tokens[j], tokens[j + len(wanted) - 1]
                    stop = re.compile(r"\S*").match(heard, last.end()).end()  # keep the word's trailing punctuation
                    return Quote(heard[first.start() : stop], start=float(run[0]["start"]), end=float(run[-1]["end"]))
    return None


def _quote_problem(text: str, footage: dict[str, Any], start: float, end: float) -> str:
    words = _words(text)
    if any(_contains(_words(s["text"]), words) for s in footage.get("unheard", [])):
        return f"quote {text!r} is an unheard line, not dialogue; never quote it"
    return f"quote {text!r} is not heard in the transcript during {start}-{end} s; quote the words exactly, or use null"


def _cast_names(cast: list[CastMember]) -> dict[str, str]:
    """Each name the Watcher may use, mapped to the character it means; a character's own name wins."""
    names = {m.name.casefold(): m.character for m in cast if m.name}
    names.update({m.character.casefold(): m.character for m in cast})
    return names
