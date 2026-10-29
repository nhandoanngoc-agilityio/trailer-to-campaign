"""Tests for sampling frames with ffmpeg: a fixed interval plus scene changes (ADR 0003)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from clips import CUT_AT, make_clip

from trailer_to_campaign.footage import frames, settings
from trailer_to_campaign.footage.frames import FootageError, extract_frames, probe, sample_times, scene_times, sha12


@pytest.fixture(scope="module")
def clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_clip(tmp_path_factory.mktemp("clip") / "trailer.mp4")


@pytest.fixture
def spy_run(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, Any]]]:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    real = subprocess.run

    def spy(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return real(args, **kwargs)

    monkeypatch.setattr(frames.subprocess, "run", spy)
    return calls


def test_probe_reads_duration_and_size(clip: Path) -> None:
    info = probe(clip)
    assert info.duration == pytest.approx(12.0, abs=0.1)
    assert (info.width, info.height) == (1280, 720)


def test_probe_refuses_something_that_is_not_a_video(tmp_path: Path) -> None:
    fake = tmp_path / "trailer.mp4"
    fake.write_text("not a video")
    with pytest.raises(FootageError, match="not a video"):
        probe(fake)


def test_probe_refuses_footage_longer_than_a_trailer(clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MAX_DURATION_S", 5)
    with pytest.raises(FootageError, match="trailer limit"):
        probe(clip)


def test_scene_change_is_found_at_the_cut(clip: Path) -> None:
    assert scene_times(clip) == [pytest.approx(CUT_AT, abs=0.1)]


def test_interval_and_scene_times_merge_without_duplicates() -> None:
    times = sample_times(duration=12.0, scenes=[6.02])
    assert [(round(t, 2), reason) for t, reason in times] == [
        (0.0, "interval"),
        (3.0, "interval"),
        (6.02, "scene"),
        (9.0, "interval"),
    ]


def test_capping_keeps_scene_changes_first() -> None:
    scenes = [10.5, 50.5, 90.5]
    times = sample_times(duration=120.0, scenes=scenes, cap=10)
    assert len(times) == 10
    assert [t for t, reason in times if reason == "scene"] == scenes
    assert [t for t, _ in times] == sorted(t for t, _ in times)


def test_capping_thins_scene_changes_when_there_are_too_many() -> None:
    times = sample_times(duration=100.0, scenes=[float(t) for t in range(1, 100)], cap=5)
    assert len(times) == 5
    assert all(reason == "scene" for _, reason in times)


def test_frames_are_jpegs_at_the_long_edge(clip: Path, tmp_path: Path) -> None:
    out = tmp_path / "frames"
    result = extract_frames(clip, [(0.0, "interval"), (CUT_AT + 0.1, "scene")], out)
    assert [f.index for f in result] == [0, 1]
    for frame in result:
        assert frame.path.parent == out
        assert frame.path.suffix == ".jpg"
        info = probe(frame.path, max_duration=None)
        assert max(info.width, info.height) == settings.LONG_EDGE_PX


def test_sha12_names_the_footage(clip: Path, tmp_path: Path) -> None:
    other = make_clip(tmp_path / "other.mp4", seconds=8.0)
    assert len(sha12(clip)) == 12
    assert sha12(clip) != sha12(other)


def test_ffmpeg_never_runs_through_a_shell(clip: Path, tmp_path: Path, spy_run: list) -> None:
    probe(clip)
    scene_times(clip)
    extract_frames(clip, [(1.0, "interval")], tmp_path / "frames")
    assert spy_run
    for args, kwargs in spy_run:
        assert isinstance(args, list)
        assert not kwargs.get("shell")
        assert kwargs.get("timeout")
        for arg in args:
            if arg.endswith((".mp4", ".jpg")):
                assert Path(arg).is_absolute()  # so a file name can never be read as an option


def test_probe_uses_the_picture_length_when_sound_runs_on(tmp_path: Path) -> None:
    clip = make_clip(tmp_path / "tail.mp4", seconds=8.0, audio_seconds=10.0)
    info = probe(clip)
    assert info.duration == pytest.approx(8.0, abs=0.1)
    times = sample_times(info.duration, scene_times(clip))
    extract_frames(clip, times, tmp_path / "frames")  # no frame asked for past the picture


def test_probe_says_whether_there_is_sound(clip: Path, tmp_path: Path) -> None:
    assert probe(clip).has_audio
    assert not probe(make_clip(tmp_path / "silent.mp4", seconds=4.0, audio=False)).has_audio


def test_a_burst_of_scene_changes_is_one_moment_and_coverage_stays_even() -> None:
    burst = [50.0 + i * 0.04 for i in range(100)]  # a strobe: 100 "cuts" in 4 s
    times = sample_times(duration=150.0, scenes=burst, cap=120)
    scene = [t for t, reason in times if reason == "scene"]
    assert len(scene) <= 9  # at most one per MIN_GAP_S across the 4 s burst
    gaps = [b - a for (a, _), (b, _) in zip(times, times[1:], strict=False)]
    assert max(gaps) <= settings.INTERVAL_S + settings.MIN_GAP_S


def test_small_sources_are_not_upscaled(tmp_path: Path) -> None:
    small = make_clip(tmp_path / "small.mp4", seconds=2.0, size="640x360")
    [frame] = extract_frames(small, [(0.5, "interval")], tmp_path / "frames")
    info = probe(frame.path, max_duration=None)
    assert (info.width, info.height) == (640, 360)
