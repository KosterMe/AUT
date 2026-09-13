"""Asking ffmpeg about a file, and rendering the things that are not the clip.

The half of AUT's `adapters/media/ffmpeg.py` that turns pixels into pixels, or
measures them. What stayed behind is the half that knows about jobs — where a
clip's file goes, what a job's thumbnail is called — because those are AUT's
questions and not this service's.

`ffmpeg_exe` and `media_root` are duplicated rather than shared. That is the
honest price of a real boundary and it is about thirty lines: bargaining it
down with a common module is the reliable way to get coupling back exactly
where it was just removed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from montage.config import get_settings
from montage.render import filters
from montage.style import PacingStyle

log = logging.getLogger(__name__)


def ffmpeg_exe() -> str | None:
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def media_root() -> str:
    """Root of the media tree, with its subdirectories guaranteed to exist."""
    paths = get_settings().paths
    for child in paths.MEDIA_SUBDIRS:
        (paths.media_dir / child).mkdir(parents=True, exist_ok=True)
    return str(paths.media_dir)


def probe_media(video_path: str) -> dict[str, Any]:
    info: dict[str, Any] = {
        "path": video_path,
        "exists": os.path.exists(video_path),
        "size_bytes": os.path.getsize(video_path) if os.path.exists(video_path) else 0,
    }
    if not info["exists"]:
        return info

    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    video_path,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=get_settings().render.ffprobe_timeout_seconds,
            )
            payload = json.loads(proc.stdout or "{}")
            return {**info, **_probe_payload_to_info(payload)}
        except (subprocess.SubprocessError, ValueError, json.JSONDecodeError):
            pass

    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        return info
    try:
        proc = subprocess.run(
            [ffmpeg, "-i", video_path],
            capture_output=True,
            text=True,
            timeout=get_settings().render.ffprobe_timeout_seconds,
        )
    except subprocess.SubprocessError:
        return info
    return {**info, **_parse_ffmpeg_probe(proc.stderr)}


def _probe_payload_to_info(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    format_info = payload.get("format") or {}
    duration = _as_float(format_info.get("duration"))
    if duration is not None:
        result["duration"] = round(duration, 3)
    has_audio = False
    for stream in payload.get("streams") or []:
        codec_type = stream.get("codec_type")
        if codec_type == "video" and "width" not in result:
            result["width"] = stream.get("width")
            result["height"] = stream.get("height")
            result["video_codec"] = stream.get("codec_name")
            result["frame_rate"] = stream.get("r_frame_rate")
        if codec_type == "audio":
            has_audio = True
            result.setdefault("audio_codec", stream.get("codec_name"))
    result["has_audio"] = has_audio
    return result


def _parse_ffmpeg_probe(stderr: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    duration_match = re.search(
        r"Duration:\s*(?P<hours>\d{2}):(?P<minutes>\d{2}):(?P<seconds>\d{2}(?:\.\d+)?)",
        stderr,
    )
    if duration_match:
        result["duration"] = round(
            int(duration_match.group("hours")) * 3600
            + int(duration_match.group("minutes")) * 60
            + float(duration_match.group("seconds")),
            3,
        )
    video_match = re.search(r"Video:\s*[^,\n]+(?:,[^,\n]+)*,\s*(?P<width>\d{2,5})x(?P<height>\d{2,5})", stderr)
    if video_match:
        result["width"] = int(video_match.group("width"))
        result["height"] = int(video_match.group("height"))
    result["has_audio"] = "Audio:" in stderr
    return result


def ffprobe_has_audio(video_path: str) -> bool:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return True
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                video_path,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=get_settings().render.ffprobe_timeout_seconds,
        )
        return bool(proc.stdout.strip())
    except subprocess.SubprocessError:
        return True


def montage_keep_segments(
    video_path: str,
    *,
    start_sec: float,
    end_sec: float,
    pacing: PacingStyle | None = None,
) -> list[tuple[float, float]]:
    """The stretches of a clip worth keeping, silence taken out.

    Every threshold arrives on the clip's own `pacing` style rather than as a
    default argument nothing ever overrode. That matters most for the noise
    floor: -35 dB is an absolute level, so a quietly recorded source has no
    silence by that measure and the montage used to do nothing at all, with
    no way to say so.
    """
    rules = pacing or PacingStyle()
    duration = max(0.1, end_sec - start_sec)
    silences = _detect_silences(
        video_path,
        start_sec=start_sec,
        duration=duration,
        noise_db=f"{rules.noise_db:.1f}dB",
        silence_seconds=rules.min_silence_seconds,
    )
    if not silences:
        return [(0.0, duration)]

    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for silence_start, silence_end in silences:
        silence_start = max(0.0, min(duration, silence_start))
        silence_end = max(silence_start, min(duration, silence_end))
        if silence_end - silence_start < rules.min_silence_seconds:
            continue

        keep_end = min(duration, silence_start + rules.padding_seconds)
        next_cursor = max(0.0, silence_end - rules.padding_seconds)
        if keep_end - cursor >= rules.min_segment_seconds:
            keep.append((round(cursor, 3), round(keep_end, 3)))
        cursor = max(cursor, next_cursor)

    if duration - cursor >= rules.min_segment_seconds:
        keep.append((round(cursor, 3), round(duration, 3)))

    if not keep:
        return [(0.0, duration)]

    # The guard: if removing silence would take out almost nothing, or would
    # leave less than half the clip standing, the montage is abandoned and the
    # clip plays as one continuous piece. Both failure modes produce a worse
    # clip than not trying.
    kept_duration = sum(end - start for start, end in keep)
    removed = duration - kept_duration
    if removed < rules.min_removed_seconds or kept_duration < min(
        10.0, duration * rules.min_kept_share
    ):
        return [(0.0, duration)]
    return keep[: rules.max_segments]


def _detect_silences(
    video_path: str,
    *,
    start_sec: float,
    duration: float,
    noise_db: str,
    silence_seconds: float,
) -> list[tuple[float, float]]:
    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        return []
    try:
        proc = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-nostats",
                "-ss",
                f"{start_sec:.3f}",
                "-t",
                f"{duration:.3f}",
                "-i",
                video_path,
                "-af",
                f"silencedetect=noise={noise_db}:d={silence_seconds:.2f}",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=get_settings().render.silence_scan_timeout_seconds,
        )
    except subprocess.SubprocessError:
        return []
    if proc.returncode not in (0, 1):
        return []
    return _parse_silencedetect(proc.stderr, duration=duration)


def _parse_silencedetect(stderr: str, *, duration: float) -> list[tuple[float, float]]:
    starts: list[float] = []
    silences: list[tuple[float, float]] = []
    for line in stderr.splitlines():
        start_match = re.search(r"silence_start:\s*([0-9.]+)", line)
        if start_match:
            starts.append(float(start_match.group(1)))
            continue
        end_match = re.search(r"silence_end:\s*([0-9.]+)", line)
        if end_match and starts:
            start = starts.pop(0)
            end = float(end_match.group(1))
            if end > start:
                silences.append((start, end))
    for start in starts:
        if duration > start:
            silences.append((start, duration))
    return silences


def render_clip_cover(
    *,
    source_path: str,
    output_path: str,
    start_sec: float = 0.0,
    title_text: str | None = None,
    part_text: str | None = None,
    thumbnail_path: str | None = None,
    width: int = 1080,
    height: int = 1920,
) -> str | None:
    """Render a vertical cover image for a clip.

    The base image is the original video's poster (``thumbnail_path``) when it is
    available, otherwise the clip's opening frame. The source title and the part
    badge are burned on with libass so Unicode/emoji render like the subtitles do.
    Returns the cover path, or ``None`` if it could not be produced (cover
    generation is best-effort and never blocks the clip render).
    """
    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        return None
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    ass_path = os.path.abspath(
        os.path.join(media_root(), "tmp", f"{Path(output_path).stem}.cover.ass")
    )
    subtitles.write_cover_ass_file(
        ass_path,
        width=width,
        height=height,
        title_text=title_text,
        part_text=part_text,
    )

    if thumbnail_path and os.path.exists(thumbnail_path):
        input_args = ["-i", thumbnail_path]
    else:
        input_args = ["-ss", filters.flt(max(0.0, start_sec)), "-i", source_path]

    filter_complex = _cover_filter(width=width, height=height, subtitle_path=ass_path)
    args = [
        ffmpeg,
        "-y",
        *input_args,
        "-frames:v",
        "1",
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-q:v",
        "3",
        output_path,
    ]
    try:
        subprocess.run(
            args,
            check=True,
            capture_output=True,
            timeout=get_settings().render.ffprobe_timeout_seconds * 3,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if not os.path.exists(output_path) or os.path.getsize(output_path) < 512:
        return None
    return output_path


def _cover_filter(*, width: int, height: int, subtitle_path: str | None) -> str:
    # Framed exactly like the clip it belongs to, zoom included — a cover that
    # crops differently from the video looks like a different video.
    render = get_settings().render
    chain = ";".join(
        [
            "[0:v]split=2[cbg][cfg]",
            filters.background(width, height, src="[cbg]", out="bg",
                               divisor=render.blur_scale_divisor),
            filters.foreground(width, height, src="[cfg]", out="fg", zoom=render.foreground_zoom),
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,"
            "eq=contrast=1.04:saturation=1.08,format=yuv420p,setsar=1",
        ]
    )
    if subtitle_path:
        chain += f",subtitles=filename='{filters.path(subtitle_path)}'{filters.fontsdir_arg()}"
    return chain + "[v]"


def cover_path_for(video_path: str) -> str:
    """Cover image beside a rendered clip: same name, .cover.jpg extension."""
    return os.path.splitext(video_path)[0] + ".cover.jpg"


def probe_render_output(
    video_path: str,
    *,
    expected_width: int,
    expected_height: int,
    expected_duration: float,
    expected_has_audio: bool,
) -> dict[str, Any]:
    info = probe_media(video_path)
    warnings: list[str] = []
    if not info.get("exists"):
        warnings.append("missing_output_file")
    if int(info.get("size_bytes") or 0) < 2048:
        warnings.append("output_file_too_small")
    duration = _as_float(info.get("duration"))
    if duration is not None and expected_duration > 0 and abs(duration - expected_duration) > max(1.5, expected_duration * 0.15):
        warnings.append("duration_mismatch")
    if info.get("width") and int(info["width"]) != expected_width:
        warnings.append("width_mismatch")
    if info.get("height") and int(info["height"]) != expected_height:
        warnings.append("height_mismatch")
    if expected_has_audio and info.get("has_audio") is False:
        warnings.append("missing_audio")
    info["expected_duration"] = round(expected_duration, 3)
    info["expected_width"] = expected_width
    info["expected_height"] = expected_height
    info["warnings"] = warnings
    info["ok"] = not warnings
    return info


def safe_filename(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._")
    return cleaned[:80] or fallback


def ffmpeg_duration(video_path: str) -> float | None:
    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        return None
    try:
        proc = subprocess.run(
            [ffmpeg, "-i", video_path],
            capture_output=True,
            text=True,
            timeout=get_settings().render.ffprobe_timeout_seconds,
        )
    except subprocess.SubprocessError:
        return None
    match = re.search(
        r"Duration:\s*(?P<hours>\d{2}):(?P<minutes>\d{2}):(?P<seconds>\d{2}(?:\.\d+)?)",
        proc.stderr,
    )
    if not match:
        return None
    return (
        int(match.group("hours")) * 3600
        + int(match.group("minutes")) * 60
        + float(match.group("seconds"))
    )


def ffprobe_duration(video_path: str) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return ffmpeg_duration(video_path)
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=get_settings().render.ffprobe_timeout_seconds,
        )
        return float(proc.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
