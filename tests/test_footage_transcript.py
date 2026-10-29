"""Tests for the trailer's timestamped speech transcript."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from trailer_to_campaign.config import load_settings
from trailer_to_campaign.footage.transcript import FasterWhisperTranscriber, Segment, Transcript


def test_transcript_round_trips_through_json_keeping_vietnamese(tmp_path: Path) -> None:
    transcript = Transcript(language="vi", segments=[Segment(0.0, 2.5, "Con sông lặng lẽ"), Segment(3.0, 4.0, "Hi")])
    path = tmp_path / "transcript.json"
    transcript.save(path)
    assert "Con sông lặng lẽ" in path.read_text(encoding="utf-8")
    assert Transcript.load(path) == transcript


def test_empty_and_backwards_segments_are_dropped() -> None:
    transcript = Transcript.from_segments(
        "en", [Segment(0.0, 1.0, "  "), Segment(2.0, 1.5, "backwards"), Segment(3.0, 4.0, " kept ")]
    )
    assert transcript.segments == [Segment(3.0, 4.0, "kept")]


class FakeModel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def transcribe(self, audio: str, **kwargs: Any) -> tuple[Any, Any]:
        self.calls.append((audio, kwargs))
        segments = iter([SimpleNamespace(start=0.4, end=2.0, text=" The river remembers. ")])
        return segments, SimpleNamespace(language="en")


def test_faster_whisper_is_given_the_file_and_its_segments_become_the_transcript(tmp_path: Path) -> None:
    model = FakeModel()
    names: list[str] = []

    def factory(name: str) -> FakeModel:
        names.append(name)
        return model

    video = tmp_path / "trailer.mp4"
    transcript = FasterWhisperTranscriber("small", model_factory=factory)(video)
    assert names == ["small"]
    assert model.calls[0][0] == str(video.resolve())
    assert transcript == Transcript("en", [Segment(0.4, 2.0, "The river remembers.")])


def test_the_model_loads_once(tmp_path: Path) -> None:
    loads = []
    transcriber = FasterWhisperTranscriber("small", model_factory=lambda name: loads.append(name) or FakeModel())
    transcriber(tmp_path / "a.mp4")
    transcriber(tmp_path / "b.mp4")
    assert loads == ["small"]


def test_whisper_model_defaults_to_small_and_can_be_overridden() -> None:
    assert load_settings({}).whisper_model == "small"
    assert load_settings({"T2C_WHISPER_MODEL": "medium"}).whisper_model == "medium"


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("say") is None, reason="needs macOS `say` to generate speech")
def test_real_whisper_hears_generated_speech(tmp_path: Path) -> None:
    speech = tmp_path / "speech.aiff"
    subprocess.run(["say", "-o", str(speech), "The quiet river is coming this winter."], check=True)
    video = tmp_path / "trailer.mp4"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=640x360:r=24", "-i", str(speech),
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video),
        ],
        check=True,
    )  # fmt: skip
    transcript = FasterWhisperTranscriber("tiny")(video)
    assert transcript.language == "en"
    assert "river" in " ".join(s.text for s in transcript.segments).lower()


def test_segments_whisper_marks_as_probably_not_speech_are_dropped(tmp_path: Path) -> None:
    # Over music or silence Whisper invents lines like "I hope you enjoyed this video";
    # a Copywriter must never quote one as dialogue.
    class MusicModel:
        def transcribe(self, audio: str, **kwargs: Any) -> tuple[Any, Any]:
            segments = [
                SimpleNamespace(start=6.5, end=8.4, text=" You", no_speech_prob=0.91),
                SimpleNamespace(start=20.0, end=22.0, text=" We ride at dawn.", no_speech_prob=0.05),
            ]
            return iter(segments), SimpleNamespace(language="en")

    transcript = FasterWhisperTranscriber("small", model_factory=lambda name: MusicModel())(tmp_path / "t.mp4")
    assert transcript.segments == [Segment(20.0, 22.0, "We ride at dawn.")]
    # Set aside, not lost: a shared no-speech score can also catch real dialogue over loud music.
    assert transcript.unheard == [Segment(6.5, 8.4, "You")]


def test_footage_with_no_speech_has_no_language(tmp_path: Path) -> None:
    class SilentModel:
        def transcribe(self, audio: str, **kwargs: Any) -> tuple[Any, Any]:
            return iter([SimpleNamespace(start=6.5, end=8.4, text=" You", no_speech_prob=0.91)]), SimpleNamespace(
                language="en"
            )

    transcript = FasterWhisperTranscriber("small", model_factory=lambda name: SilentModel())(tmp_path / "t.mp4")
    assert transcript == Transcript(None, [], unheard=[Segment(6.5, 8.4, "You")])


def test_unheard_segments_round_trip_and_old_files_still_load(tmp_path: Path) -> None:
    transcript = Transcript("en", [Segment(1.0, 2.0, "Hi")], unheard=[Segment(5.0, 6.0, "You")])
    path = tmp_path / "transcript.json"
    transcript.save(path)
    assert Transcript.load(path) == transcript
    path.write_text('{"language": "en", "segments": []}', encoding="utf-8")  # written before unheard existed
    assert Transcript.load(path) == Transcript("en", [])
