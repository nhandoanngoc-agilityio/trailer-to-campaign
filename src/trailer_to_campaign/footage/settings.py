"""Footage limits and sampling defaults, in one place.

Read them through this module (settings.MAX_BYTES) rather than importing the
names, so a test can lower a limit without generating a huge file.
"""

from __future__ import annotations

# What counts as a trailer source (ADR 0005: official trailers only).
ALLOWED_HOSTS = ("youtube.com", "youtu.be", "vimeo.com")
ALLOWED_EXTRACTORS = ["youtube", "vimeo"]  # yt-dlp: single videos only, never its generic extractor
MAX_DURATION_S = 10 * 60
MAX_BYTES = 500 * 1024 * 1024

# Sampling (ADR 0003): a frame every INTERVAL_S plus one at each scene change.
INTERVAL_S = 3.0
SCENE_THRESHOLD = 0.4
MAX_FRAMES = 120
LONG_EDGE_PX = 768
MIN_GAP_S = 0.5  # frames closer than this are the same moment

# Over music or silence Whisper invents speech ("I hope you enjoyed this video").
# Segments it scores as more likely not speech than this are dropped.
NO_SPEECH_MAX = 0.6

# A ceiling on any one ffmpeg or ffprobe call.
SUBPROCESS_TIMEOUT_S = 300
