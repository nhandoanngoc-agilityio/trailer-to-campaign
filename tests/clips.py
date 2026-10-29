"""A generated stand-in for a trailer, shared by the footage tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

CUT_AT = 6.0


def make_clip(path: Path, seconds: float = 12.0) -> Path:
    """Red, then a hard cut to blue at CUT_AT, with a tone: a stand-in for a trailer."""
    half = seconds / 2
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=red:s=1280x720:r=24:d={half}",
            "-f", "lavfi", "-i", f"color=c=blue:s=1280x720:r=24:d={half}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(path),
        ],
        check=True,
    )  # fmt: skip
    return path
