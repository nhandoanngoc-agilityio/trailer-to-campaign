"""Tests for grounding Key Moments in the footage and the Catalog Metadata."""

from __future__ import annotations

from typing import Any

import pytest

from trailer_to_campaign.agents.key_moments import KeyMoment, NotGrounded, Quote, ground
from trailer_to_campaign.catalog import CastMember

FOOTAGE: dict[str, Any] = {
    "duration": 30.0,
    "frames": [
        {"index": i, "t": 3.0 * i, "reason": "interval", "file": f"frames/x/frame_{i:04d}.jpg"} for i in range(10)
    ],
    "transcript": [
        {"start": 0.5, "end": 2.0, "text": "Agent 327, you're late."},
        {"start": 10.0, "end": 12.0, "text": "The barber knows"},
        {"start": 12.2, "end": 13.0, "text": "nothing more."},
    ],
    "unheard": [{"start": 20.0, "end": 21.0, "text": "Thanks for watching!"}],
}
CAST = [CastMember("Agent 327"), CastMember("Boris Kloris", name="Jane Actor")]
ALL_FRAMES = set(range(10))


def moment(start: float, end: float, **over: Any) -> dict[str, Any]:
    first = int(start // 3) + (start % 3 > 0)
    return {
        "start": start,
        "end": end,
        "what_happens": "Something on screen.",
        "mood": "tense",
        "quote": None,
        "characters": [],
        "frames": [first],
        **over,
    }


def five(**first: Any) -> list[dict[str, Any]]:
    return [
        moment(0.0, 2.5, **first),
        moment(6.0, 8.0),
        moment(9.0, 13.0),
        moment(15.0, 17.0),
        moment(24.0, 27.0),
    ]


def problems(moments: list[dict[str, Any]], seen: set[int] = ALL_FRAMES) -> list[str]:
    with pytest.raises(NotGrounded) as caught:
        ground(moments, FOOTAGE, CAST, seen)
    return caught.value.problems


def test_a_grounded_set_is_accepted_and_put_in_order() -> None:
    moments = five(quote="Agent 327, you're late.", characters=["agent 327"])
    grounded = ground(list(reversed(moments)), FOOTAGE, CAST, ALL_FRAMES)
    assert [m.start for m in grounded] == [0.0, 6.0, 9.0, 15.0, 24.0]
    assert grounded[0] == KeyMoment(
        start=0.0,
        end=2.5,
        what_happens="Something on screen.",
        mood="tense",
        quote=Quote("Agent 327, you're late.", start=0.5, end=2.0),
        characters=["Agent 327"],  # the catalog's spelling
        frames=[0],
    )


@pytest.mark.parametrize("count", [4, 11])
def test_fewer_than_5_or_more_than_10_are_refused(count: int) -> None:
    moments = [moment(3.0 * (i % 10), 3.0 * (i % 10) + 0.5, frames=[i % 10]) for i in range(count)]
    [problem] = problems(moments)
    assert "5 to 10" in problem and str(count) in problem


@pytest.mark.parametrize(
    ("start", "end", "frame", "says"),
    [(-1.0, 2.0, 0, "30.0"), (28.0, 31.0, 9, "30.0"), (8.0, 6.0, 2, "before"), (6.0, 6.0, 2, "before")],
)
def test_a_time_outside_the_trailer_or_backwards_is_refused(start: float, end: float, frame: int, says: str) -> None:
    [problem] = problems(five()[:4] + [moment(start, end, frames=[frame])])  # bad times: no frame or quote checks
    assert problem.startswith("Key Moment 5:") and says in problem


def test_a_frame_not_looked_at_is_refused() -> None:
    [problem] = problems(five(), seen=ALL_FRAMES - {2})
    assert "frame 2" in problem and "get_frame" in problem


def test_a_frame_that_does_not_exist_is_refused() -> None:
    [problem] = problems(five(frames=[0, 42]))
    assert "frame 42" in problem


def test_a_frame_outside_the_moment_is_refused() -> None:
    [problem] = problems(five(frames=[5]))  # t=15.0, the moment is 0-2.5 s
    assert "frame 5" in problem and "15.0" in problem


def test_a_moment_with_no_frame_is_refused() -> None:
    [problem] = problems(five(frames=[]))
    assert "frame" in problem


def test_a_quote_heard_elsewhere_in_the_trailer_is_refused() -> None:
    [problem] = problems(five(quote="The barber knows"))  # said at 10 s, the moment is 0-2.5 s
    assert "quote" in problem


def test_an_invented_quote_is_refused() -> None:
    [problem] = problems(five(quote="Nobody leaves this barbershop."))
    assert "quote" in problem


def test_an_unheard_line_is_never_a_quote() -> None:
    moments = five()
    moments[4] = moment(19.0, 22.0, frames=[7], quote="Thanks for watching!")
    [problem] = problems(moments)
    assert "unheard" in problem


def test_a_quote_matches_despite_case_and_punctuation_and_may_span_two_lines() -> None:
    moments = five()
    moments[2] = moment(9.0, 13.0, quote="the BARBER knows... nothing more")
    grounded = ground(moments, FOOTAGE, CAST, ALL_FRAMES)
    assert grounded[2].quote == Quote("The barber knows nothing more.", start=10.0, end=13.0)  # as heard


@pytest.mark.parametrize(
    ("submitted", "stored"),
    [("AGENT 327 YOU'RE LATE!", "Agent 327, you're late."), ("you're late", "you're late.")],
)
def test_a_quote_is_stored_in_the_transcripts_words_not_the_watchers(submitted: str, stored: str) -> None:
    grounded = ground(five(quote=submitted), FOOTAGE, CAST, ALL_FRAMES)
    assert grounded[0].quote == Quote(stored, start=0.5, end=2.0)


def test_a_quote_must_be_whole_words() -> None:
    [problem] = problems(five(quote="ent 327"))
    assert "quote" in problem


def test_a_character_not_in_the_catalog_is_refused() -> None:
    [problem] = problems(five(characters=["Agent 327", "Dr. Kloris"]))
    assert "Dr. Kloris" in problem
    assert "Agent 327" in problem and "Boris Kloris" in problem  # the allowed names


def test_an_actor_name_from_the_catalog_names_their_character() -> None:
    grounded = ground(five(characters=["jane actor"]), FOOTAGE, CAST, ALL_FRAMES)
    assert grounded[0].characters == ["Boris Kloris"]


def test_a_character_name_wins_over_an_actor_with_the_same_name() -> None:
    cast = [CastMember("Max"), CastMember("Boris Kloris", name="Max")]
    grounded = ground(five(characters=["max"]), FOOTAGE, cast, ALL_FRAMES)
    assert grounded[0].characters == ["Max"]


# Over the frame cap, interval frames are thinned (footage.settings.MAX_FRAMES): here one every 6 s.
SPARSE: dict[str, Any] = {
    **FOOTAGE,
    "duration": 60.0,
    "frames": [{"index": i, "t": 6.0 * i, "reason": "interval", "file": f"f{i}.jpg"} for i in range(10)],
}


def test_thinned_frames_widen_the_window_to_the_nearest_frame() -> None:
    moments = [moment(2.0, 3.0, frames=[0]), moment(14.0, 15.0, frames=[2])]  # frame 2 is at 12 s
    moments += [moment(20.0, 21.0, frames=[3]), moment(26.0, 27.0, frames=[4]), moment(38.0, 39.0, frames=[6])]
    assert len(ground(moments, SPARSE, CAST, ALL_FRAMES)) == 5
    moments[1] = moment(14.0, 15.0, frames=[5])  # 30 s: still too far
    with pytest.raises(NotGrounded, match="frame 5"):
        ground(moments, SPARSE, CAST, ALL_FRAMES)


@pytest.mark.parametrize("field", ["what_happens", "mood"])
def test_empty_text_is_refused(field: str) -> None:
    [problem] = problems(five(**{field: "  "}))
    assert field in problem


def test_a_malformed_moment_is_a_problem_not_a_crash() -> None:
    moments: list[Any] = five()
    moments[1] = {"start": "soon"}
    assert any("Key Moment 2" in p for p in problems(moments))
