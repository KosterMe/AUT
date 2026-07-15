"""Measuring where a video cuts and how loud it is.

Both signals come out of one ffmpeg pass, because the cost here is decoding,
not filtering: a full decode of AV1 1080p60 runs at about 37x real time on this
machine — a two-hour film is marked up in roughly three minutes — and doing it
twice would double exactly the part that is expensive. Downscaling before the
detector barely helps (43x), for the same reason.

The parser is separate from the subprocess on purpose. ffmpeg's metadata
printer has a shape of its own, and a shape is worth testing against recorded
output rather than against a live encoder.
"""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field

from app.adapters.media.ffmpeg import ffmpeg_exe, ffprobe_has_audio
from app.core.config import get_settings

log = logging.getLogger(__name__)

SCENE_KEY = "lavfi.scene_score"
LOUDNESS_KEY = "lavfi.astats.Overall.RMS_level"

_BLOCK = re.compile(r"^frame:\s*\d+\s+pts:\s*\S+\s+pts_time:\s*(?P<time>[-\d.]+)")
_VALUE = re.compile(r"^(?P<key>[\w.]+)=(?P<value>\S+)")


@dataclass(frozen=True)
class SceneAnalysis:
    """What one pass over a source found."""

    scene_changes: tuple[float, ...] = ()
    # (second, RMS level in dB). One entry per second of audio.
    loudness: tuple[tuple[float, float], ...] = ()
    ok: bool = True
    detail: str = ""

    @property
    def cuts_per_minute(self) -> float:
        span = self.loudness[-1][0] if self.loudness else 0.0
        return len(self.scene_changes) / (span / 60.0) if span >= 60.0 else 0.0


def analyse(
    video_path: str,
    *,
    threshold: float | None = None,
    timeout_seconds: float | None = None,
) -> SceneAnalysis:
    """Find every cut in the picture, and the level of each second of audio.

    Never raises: a source that cannot be analysed produces an empty result,
    and the cutter above falls back to fixed-length pieces. Losing the scene
    signal should cost worse cut points, not the job.
    """
    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        return SceneAnalysis(ok=False, detail="ffmpeg is not installed or not on PATH")

    settings = get_settings().scenes
    limit = float(threshold if threshold is not None else settings.threshold)
    has_audio = ffprobe_has_audio(video_path)

    args = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "warning", "-i", video_path]
    graph = [f"[0:v]select='gt(scene,{limit:.3f})',metadata=print:file=-[v]"]
    maps = ["-map", "[v]"]
    if has_audio:
        # One measurement per second: asetnsamples fixes the window, which
        # astats' own `reset` counts in decoder frames rather than in time.
        graph.append(
            "[0:a]aformat=sample_rates=48000,asetnsamples=n=48000:p=0,"
            f"astats=metadata=1:reset=1,ametadata=print:key={LOUDNESS_KEY}:file=-[a]"
        )
        maps += ["-map", "[a]"]
    args += ["-filter_complex", ";".join(graph), *maps, "-f", "null", "-"]

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds or settings.timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        log.warning("scene analysis timed out on %s", video_path)
        return SceneAnalysis(ok=False, detail="scene analysis timed out")
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("scene analysis failed on %s: %s", video_path, exc)
        return SceneAnalysis(ok=False, detail=str(exc))

    if proc.returncode != 0:
        return SceneAnalysis(ok=False, detail=(proc.stderr or "").strip()[:500])

    analysis = parse_metadata(proc.stdout)
    log.info(
        "analysed %s: %d scene change(s), %d second(s) of audio measured",
        video_path, len(analysis.scene_changes), len(analysis.loudness),
    )
    return analysis


def parse_metadata(output: str) -> SceneAnalysis:
    """Read ffmpeg's metadata printer.

    It emits a header line naming the frame's time, then one line per metadata
    key on it. The video and audio branches interleave, which is harmless:
    every block carries its own timestamp, so values are attributed by position
    in the stream rather than by which filter produced them.
    """
    scene_changes: list[float] = []
    loudness: list[tuple[float, float]] = []
    at: float | None = None

    for line in output.splitlines():
        line = line.strip()
        block = _BLOCK.match(line)
        if block:
            at = _as_float(block.group("time"))
            continue
        value = _VALUE.match(line)
        if not value or at is None:
            continue
        if value.group("key") == SCENE_KEY:
            scene_changes.append(round(at, 3))
        elif value.group("key") == LOUDNESS_KEY:
            level = _as_float(value.group("value"))
            # A digital-silence window reports -inf; it is real information —
            # the quietest thing there is — so it is kept, not dropped.
            loudness.append((round(at, 3), -120.0 if level is None else level))

    return SceneAnalysis(
        scene_changes=tuple(sorted(set(scene_changes))),
        loudness=tuple(loudness),
    )


def _as_float(value: str) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return None if parsed != parsed or parsed in (float("inf"), float("-inf")) else parsed


__all__ = ["SceneAnalysis", "analyse", "parse_metadata"]
