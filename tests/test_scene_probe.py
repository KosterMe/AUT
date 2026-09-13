"""Reading ffmpeg's metadata printer.

The samples below are real output from ffmpeg 8.1.1, not invented: the printer
has a shape of its own, and inventing it is how a parser passes its tests and
fails on the first film.
"""
from __future__ import annotations

from app.adapters.media import scenes

SCENE_OUTPUT = """\
frame:0    pts:562176  pts_time:36.6
lavfi.scene_score=0.341106
frame:1    pts:1240000 pts_time:80.75
lavfi.scene_score=0.812004
"""

LOUDNESS_OUTPUT = """\
frame:0    pts:0       pts_time:0
lavfi.astats.Overall.RMS_level=-17.277044
frame:1    pts:48000   pts_time:1
lavfi.astats.Overall.RMS_level=-19.790364
"""


def test_scene_changes_are_read_with_their_timestamps():
    analysis = scenes.parse_metadata(SCENE_OUTPUT)

    assert analysis.scene_changes == (36.6, 80.75)


def test_loudness_is_read_as_one_measurement_per_second():
    analysis = scenes.parse_metadata(LOUDNESS_OUTPUT)

    assert analysis.loudness == ((0.0, -17.277044), (1.0, -19.790364))


def test_both_signals_come_out_of_one_interleaved_stream():
    """The two filters write to the same pipe. Every block carries its own
    timestamp, so values are attributed by position rather than by filter."""
    analysis = scenes.parse_metadata(LOUDNESS_OUTPUT + SCENE_OUTPUT + LOUDNESS_OUTPUT)

    assert analysis.scene_changes == (36.6, 80.75)
    assert len(analysis.loudness) == 4


def test_digital_silence_is_kept_as_the_quietest_value():
    """-inf is real information — it is the quietest thing there is — so it
    must not be dropped, or a silent stretch would rank as unmeasured."""
    analysis = scenes.parse_metadata(
        "frame:0    pts:0       pts_time:12\nlavfi.astats.Overall.RMS_level=-inf\n"
    )

    assert analysis.loudness == ((12.0, -120.0),)


def test_a_repeated_timestamp_is_counted_once():
    analysis = scenes.parse_metadata(SCENE_OUTPUT + SCENE_OUTPUT)

    assert analysis.scene_changes == (36.6, 80.75)


def test_noise_around_the_metadata_is_ignored():
    analysis = scenes.parse_metadata(
        "[Parsed_metadata_1 @ 0x55] some log line\n"
        + SCENE_OUTPUT
        + "frame= 1234 fps=180 q=-1.0 size=N/A time=00:00:41.00\n"
    )

    assert analysis.scene_changes == (36.6, 80.75)


def test_empty_output_is_an_empty_analysis_not_a_failure():
    analysis = scenes.parse_metadata("")

    assert analysis.scene_changes == ()
    assert analysis.loudness == ()
    assert analysis.ok is True


def test_a_missing_ffmpeg_reports_why_instead_of_raising(monkeypatch):
    """Losing the scene signal should cost worse cut points, not the job."""
    monkeypatch.setattr(scenes.montage, "renderer_available", lambda: None)

    analysis = scenes.analyse("/media/film.mkv")

    assert analysis.ok is False
    assert "ffmpeg" in analysis.detail


def test_a_failed_probe_reports_why_instead_of_raising(monkeypatch):
    monkeypatch.setattr(scenes.montage, "renderer_available", lambda: "ffmpeg")
    monkeypatch.setattr(scenes.montage, "has_audio", lambda path: False)

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "Invalid data found when processing input"

    monkeypatch.setattr(scenes.subprocess, "run", lambda *a, **k: Failed())

    analysis = scenes.analyse("/media/broken.mkv")

    assert analysis.ok is False
    assert "Invalid data" in analysis.detail


def test_a_source_without_audio_is_analysed_for_scenes_alone(monkeypatch):
    monkeypatch.setattr(scenes.montage, "renderer_available", lambda: "ffmpeg")
    monkeypatch.setattr(scenes.montage, "has_audio", lambda path: False)
    seen: dict[str, list[str]] = {}

    class Done:
        returncode = 0
        stdout = SCENE_OUTPUT
        stderr = ""

    def fake_run(args, **kwargs):
        seen["args"] = args
        return Done()

    monkeypatch.setattr(scenes.subprocess, "run", fake_run)
    analysis = scenes.analyse("/media/silent.mkv")

    graph = seen["args"][seen["args"].index("-filter_complex") + 1]
    assert "astats" not in graph
    assert analysis.scene_changes == (36.6, 80.75)


def test_the_threshold_reaches_the_filter(monkeypatch):
    monkeypatch.setattr(scenes.montage, "renderer_available", lambda: "ffmpeg")
    monkeypatch.setattr(scenes.montage, "has_audio", lambda path: True)
    seen: dict[str, list[str]] = {}

    class Done:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(scenes.subprocess, "run", lambda args, **k: (seen.setdefault("args", args), Done())[1])
    scenes.analyse("/media/film.mkv", threshold=0.25)

    graph = seen["args"][seen["args"].index("-filter_complex") + 1]
    assert "gt(scene,0.250)" in graph
