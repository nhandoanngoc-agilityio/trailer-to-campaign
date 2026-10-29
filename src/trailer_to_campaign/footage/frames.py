"""Sample frames from a trailer with ffmpeg: a fixed interval plus scene changes (ADR 0003).

Every ffmpeg and ffprobe call goes through run(): an argument list, never a
shell, with a timeout. File paths are passed absolute, so a file name can never
start with "-" and be read as an option.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from trailer_to_campaign.footage import settings

Reason = Literal["interval", "scene"]

_PTS_TIME = re.compile(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")


class FootageError(RuntimeError):
    """ffmpeg could not do what was asked, or the footage is not a usable trailer."""


@dataclass(frozen=True)
class Probe:
    duration: float  # of the picture: sound can run on past the last frame
    width: int
    height: int
    has_audio: bool = False


@dataclass(frozen=True)
class Frame:
    index: int
    t: float
    reason: Reason
    path: Path


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    if shutil.which(args[0]) is None:
        raise FootageError(f"{args[0]} is not installed")
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=settings.SUBPROCESS_TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired as err:
        raise FootageError(f"{args[0]} took longer than {settings.SUBPROCESS_TIMEOUT_S} s") from err
    if result.returncode != 0:
        tail = result.stderr.strip().splitlines()[-1:] or ["no output"]
        raise FootageError(f"{args[0]} failed: {tail[0]}")
    return result


def _abs(path: Path) -> str:
    return str(path.resolve())


_USE_SETTINGS = -1.0


def probe(path: Path, max_duration: float | None = _USE_SETTINGS) -> Probe:
    """Read a video's picture length, size and whether it has sound.

    max_duration=None skips the trailer length check.
    """
    try:
        result = run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "stream=codec_type,width,height,duration:format=duration", "-of", "json", _abs(path),
            ]
        )  # fmt: skip
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        video = next(s for s in streams if s.get("codec_type") == "video")
        width, height = int(video["width"]), int(video["height"])
    except (FootageError, ValueError, KeyError, StopIteration) as err:
        raise FootageError(f"{path.name} is not a video") from err
    lengths = [_seconds(video.get("duration")), _seconds(data.get("format", {}).get("duration"))]
    known = [d for d in lengths if d > 0]
    duration = min(known) if known else 0.0
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    limit = settings.MAX_DURATION_S if max_duration == _USE_SETTINGS else max_duration
    if limit is not None:
        if duration <= 0:
            raise FootageError(f"{path.name} has no readable length")
        if duration > limit:
            raise FootageError(f"{path.name} is {duration:.0f} s, over the {limit:.0f} s trailer limit")
    return Probe(duration, width, height, has_audio)


def _seconds(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def scene_times(path: Path, threshold: float | None = None) -> list[float]:
    """Timestamps where the picture changes by more than threshold (0 to 1)."""
    threshold = settings.SCENE_THRESHOLD if threshold is None else threshold
    result = run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-i", _abs(path), "-an",
            "-vf", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-",
        ]
    )  # fmt: skip
    return [float(m.group(1)) for m in _PTS_TIME.finditer(result.stderr)]


def _thin(times: list[float], k: int) -> list[float]:
    """Keep k of the times, evenly spread, including the first and last."""
    if k <= 0:
        return []
    if len(times) <= k:
        return list(times)
    if k == 1:
        return [times[0]]
    return [times[round(i * (len(times) - 1) / (k - 1))] for i in range(k)]


def sample_times(
    duration: float,
    scenes: list[float],
    interval: float | None = None,
    cap: int | None = None,
    min_gap: float | None = None,
) -> list[tuple[float, Reason]]:
    """Which moments to take a frame at. Scene changes win over interval frames when capping.

    Scene changes closer than min_gap are one moment (a strobe or a fast montage
    must not use up the cap), and an interval frame is dropped only when a kept
    scene frame sits next to it, so coverage stays even.
    """
    interval = settings.INTERVAL_S if interval is None else interval
    cap = settings.MAX_FRAMES if cap is None else cap
    min_gap = settings.MIN_GAP_S if min_gap is None else min_gap
    last = duration - settings.END_MARGIN_S  # seeking to the very end can find no picture

    scene: list[float] = []
    for t in sorted(t for t in scenes if 0 <= t < last):
        if not scene or t - scene[-1] >= min_gap:
            scene.append(t)
    chosen_scene = _thin(scene, cap)

    steps = int(duration // interval) + 1
    regular = [i * interval for i in range(steps) if i * interval < last]
    regular = [t for t in regular if all(abs(t - s) >= min_gap for s in chosen_scene)]
    chosen_regular = _thin(regular, cap - len(chosen_scene))
    merged: list[tuple[float, Reason]] = [(t, "scene") for t in chosen_scene]
    merged += [(t, "interval") for t in chosen_regular]
    return sorted(merged)


def extract_frames(
    path: Path, times: list[tuple[float, Reason]], out_dir: Path, long_edge: int | None = None
) -> list[Frame]:
    long_edge = settings.LONG_EDGE_PX if long_edge is None else long_edge
    out_dir.mkdir(parents=True, exist_ok=True)
    # Shrink to fit a long_edge box, never enlarge: an upscaled frame costs more image tokens and adds nothing.
    scale = f"scale=w='min({long_edge},iw)':h='min({long_edge},ih)':force_original_aspect_ratio=decrease"
    result = []
    for index, (t, reason) in enumerate(times):
        out = out_dir / f"frame_{index:04d}.jpg"
        run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{t:.3f}", "-i", _abs(path), "-frames:v", "1", "-vf", scale, "-q:v", "3", _abs(out),
            ]
        )  # fmt: skip
        if not out.is_file():
            raise FootageError(f"ffmpeg wrote no frame at {t:.2f} s")
        result.append(Frame(index, t, reason, out))
    return result


def sha12(path: Path) -> str:
    """The first 12 hex characters of the file's sha256: names its frames folder."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:12]
