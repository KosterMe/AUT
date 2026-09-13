"""Where a job's files go, and how long its source runs.

What is left of this file after the montage service took the other half. The
split is by what a function has to know: everything here knows about *jobs* —
which directory a clip of job 12 is written to, what its source thumbnail is
called — and that is AUT's business, not the renderer's.

Everything that measures or produces pixels moved to `montage.render.probe`.
`ffmpeg_exe` and `media_root` exist on both sides now, some thirty lines of
duplicated plumbing, which is what a boundary costs when it is real.
"""
from __future__ import annotations

import json
import os
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.core.config import get_settings

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


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
