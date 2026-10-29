"""The trailer's speech as timestamped text, transcribed locally with faster-whisper.

Transcription runs on this machine, so the footage's audio never goes to a
model provider and costs nothing against the Film Budget. The transcript is
data for the Watcher, never instructions. Segments Whisper itself scores as
probably not speech are dropped, so music is never transcribed as dialogue.
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

    @classmethod
    def from_segments(cls, language: str | None, segments: Iterable[Segment]) -> Transcript:
        kept = [Segment(s.start, s.end, s.text.strip()) for s in segments if s.text.strip() and s.end > s.start]
        return cls(language, kept)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Transcript:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data["language"], [Segment(**s) for s in data["segments"]])


class Transcriber(Protocol):
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

    def __call__(self, video: Path) -> Transcript:
        if self._model is None:
            self._model = self.model_factory(self.model_name)
        segments, info = self._model.transcribe(str(video.resolve()), vad_filter=True)
        heard = (s for s in segments if (getattr(s, "no_speech_prob", 0.0) or 0.0) <= settings.NO_SPEECH_MAX)
        transcript = Transcript.from_segments(info.language, (Segment(s.start, s.end, s.text) for s in heard))
        # With no speech left, Whisper's language guess is about the music: drop it.
        return transcript if transcript.segments else Transcript(None, [])
