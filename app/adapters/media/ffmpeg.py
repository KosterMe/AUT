from __future__ import annotations

import json
import os
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.adapters.media import filters
from app.core.config import get_settings
from app.domain.style import PacingStyle
from app.domain import subtitles

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


def safe_filename(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._")
    return cleaned[:80] or fallback


def clip_output_path(job_id: int, *, index: int, start_sec: float, end_sec: float, title: str) -> str:
    """Where a rendered clip goes.

    The name carries the job, the position and the time range so a file found
    on disk can be traced back to its clip without consulting the database.
    """
    directory = os.path.join(media_root(), "slices", f"job-{job_id}")
    os.makedirs(directory, exist_ok=True)
    stem = safe_filename(title, f"clip-{index:03d}")
    return os.path.abspath(
        os.path.join(directory, f"clip-{index:03d}-{int(start_sec)}-{int(end_sec)}-{stem}.mp4")
    )


def preview_output_path(clip_id: int) -> str:
    """Where a clip's preview goes. One file per clip, overwritten each time.

    Under `tmp/`, which the retention sweep already clears: a preview is worth
    keeping exactly as long as the person who asked for it is looking at it.
    """
    directory = os.path.join(media_root(), "tmp", "previews")
    os.makedirs(directory, exist_ok=True)
    return os.path.abspath(os.path.join(directory, f"clip-{clip_id}.mp4"))


def cover_path_for(video_path: str) -> str:
    """Cover image beside a rendered clip: same name, .cover.jpg extension."""
    return os.path.splitext(video_path)[0] + ".cover.jpg"


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


def prepare_source_thumbnail(job_id: int, url: str | None) -> str | None:
    """Fetch the source video's poster image once, so covers can reuse it.

    ``url`` is the ``thumbnail`` field from yt-dlp metadata (or a local path).
    Returns a local file path, or ``None`` when no usable thumbnail exists. The
    bytes are stored raw; ffmpeg detects the real format (jpg/webp/png) on read.
    """
    if not url:
        return None
    if not str(url).lower().startswith(("http://", "https://")):
        return os.path.abspath(url) if os.path.exists(url) else None

    dest = os.path.abspath(os.path.join(media_root(), "originals", f"job-{job_id}.thumb"))
    if os.path.exists(dest) and os.path.getsize(dest) > 512:
        return dest
    try:
        import urllib.request

        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(
            request, timeout=get_settings().render.ffprobe_timeout_seconds
        ) as response:
            data = response.read()
    except Exception:
        return None
    if not data or len(data) < 512:
        return None
    with open(dest, "wb") as handle:
        handle.write(data)
    return dest


def best_thumbnail_url(metadata: dict[str, Any]) -> str | None:
    """Pick the largest still image from yt-dlp metadata (skips storyboards)."""
    url = metadata.get("thumbnail")
    if isinstance(url, str) and url.strip():
        return url.strip()
    thumbnails = metadata.get("thumbnails")
    if not isinstance(thumbnails, list):
        return None
    best: tuple[int, str] | None = None
    for item in thumbnails:
        if not isinstance(item, dict):
            continue
        candidate = str(item.get("url") or "").strip()
        if not candidate or "storyboard" in candidate.lower():
            continue
        area = int(_as_float(item.get("width")) or 0) * int(_as_float(item.get("height")) or 0)
        if best is None or area > best[0]:
            best = (area, candidate)
    return best[1] if best else None


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


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
