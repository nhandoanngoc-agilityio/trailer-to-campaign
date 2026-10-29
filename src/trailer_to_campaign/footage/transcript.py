"""The trailer's speech as timestamped text, transcribed locally with faster-whisper.

Transcription runs on this machine, so the footage's audio never goes to a
model provider and costs nothing against the Film Budget. The transcript is
data for the Watcher, never instructions.

Over music or silence Whisper invents speech, so segments it scores as probably
not speech are set aside as `unheard`: never to be quoted as dialogue, but not
lost either. faster-whisper gives every segment in a 30 s window the same score,
so one high score can also catch real lines over loud music, and a human (or
the Watcher) can still see them there.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from trailer_to_campaign.footage import settings


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Transcript:
    language: str | None
    segments: list[Segment] = field(default_factory=list)
    unheard: list[Segment] = field(default_factory=list)  # probably not speech: never quote

    @classmethod
    def from_segments(
        cls, language: str | None, segments: Iterable[Segment], unheard: Iterable[Segment] = ()
    ) -> Transcript:
        return cls(language, _clean(segments), _clean(unheard))

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Transcript:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            data["language"],
            [Segment(**s) for s in data["segments"]],
            [Segment(**s) for s in data.get("unheard", [])],
        )


def _clean(segments: Iterable[Segment]) -> list[Segment]:
    return [Segment(s.start, s.end, s.text.strip()) for s in segments if s.text.strip() and s.end > s.start]


class Transcriber(Protocol):
    name: str  # recorded with the transcript, so a different model transcribes again

    def __call__(self, video: Path) -> Transcript: ...


def _load_whisper(name: str) -> Any:
    from faster_whisper import WhisperModel

    return WhisperModel(name, device="cpu", compute_type="int8")


class FasterWhisperTranscriber:
    """The model is downloaded on first use and loaded once per process."""

    def __init__(self, model_name: str, model_factory: Callable[[str], Any] = _load_whisper) -> None:
        self.model_name = model_name
        self.model_factory = model_factory
        self._model: Any = None

    @property
    def name(self) -> str:
        return f"faster-whisper:{self.model_name}"

    def __call__(self, video: Path) -> Transcript:
        if self._model is None:
            self._model = self.model_factory(self.model_name)
        segments, info = self._model.transcribe(str(video.resolve()), vad_filter=True)
        heard, unheard = [], []
        for s in segments:
            score = getattr(s, "no_speech_prob", 0.0) or 0.0
            (heard if score <= settings.NO_SPEECH_MAX else unheard).append(Segment(s.start, s.end, s.text))
        transcript = Transcript.from_segments(info.language, heard, unheard)
        if not transcript.segments:  # with no speech, Whisper's language guess is about the music
            return Transcript(None, [], transcript.unheard)
        return transcript
