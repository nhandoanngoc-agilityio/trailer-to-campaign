"""A generated stand-in for a trailer, shared by the footage tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

CUT_AT = 6.0


def make_clip(
    path: Path,
    seconds: float = 12.0,
    size: str = "1280x720",
    audio_seconds: float | None = None,
    audio: bool = True,
) -> Path:
    """Red, then a hard cut to blue halfway, with a tone: a stand-in for a trailer.

    audio_seconds longer than seconds gives a tail of sound with no picture, as
    trailers often end; audio=False gives a silent teaser with no audio track.
    """
    half = seconds / 2
    args = [
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=red:s={size}:r=24:d={half}",
        "-f", "lavfi", "-i", f"color=c=blue:s={size}:r=24:d={half}",
    ]  # fmt: skip
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={audio_seconds or seconds}"]
    args += ["-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]"]
    if audio:
        args += ["-map", "2:a", "-c:a", "aac"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(args, check=True)
    return path
