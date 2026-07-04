"""Fetching a source video with yt-dlp.

The single download path in the application. The previous version had two:
this one, and `Video.get_youtube_video`, which wrote every download to the
same fixed `VideosDirPath/pre-processed.mp4` — so two concurrent jobs
overwrote each other's file. Output here is always keyed by job id.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from app.adapters.media.ffmpeg import ffprobe_duration, media_root
from app.adapters.youtube.options import (
    apply_ytdlp_auth_options,
    assert_downloadable_formats,
    explain_ytdlp_auth_error,
)
from app.core.config import get_settings

log = logging.getLogger(__name__)

# Called with how much of the current file has arrived, from 0.0 to 1.0.
ProgressCallback = Callable[[float], None]

# A slow source can take twenty minutes. Often enough to prove the download
# is alive, rare enough that the reporting is not part of the cost.
_PROGRESS_INTERVAL_SECONDS = 2.0


def extract_info(source_ref: str, *, get_comments: bool = False) -> dict[str, Any]:
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": get_settings().youtube.socket_timeout,
    }
    if get_comments:
        opts["getcomments"] = True
    try:
        apply_ytdlp_auth_options(opts)
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(source_ref, download=False)
    except Exception as exc:
        raise RuntimeError(explain_ytdlp_auth_error(exc)) from exc


def download_source(
    source_ref: str, *, job_id: int, on_progress: ProgressCallback | None = None
) -> tuple[str, dict[str, Any]]:
    import yt_dlp

    root = media_root()
    originals_dir = os.path.join(root, "originals")
    outtmpl = os.path.join(root, "originals", f"job-{job_id}.%(ext)s")
    cached = [
        path
        for path in _download_candidates({}, originals_dir, job_id)
        if not _is_format_intermediate(path, job_id)
    ]
    if cached:
        return os.path.abspath(cached[0]), _cached_metadata(source_ref, cached[0])

    preflight_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "ignore_no_formats_error": True,
        "file_access_retries": 10,
        "socket_timeout": get_settings().youtube.socket_timeout,
    }
    opts = {
        "format": "bestvideo*+bestaudio/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # Resume a part file instead of starting over. This is yt-dlp's own
        # default, and turning it off costs everything already fetched every
        # time the connection drops — on a link that severs the transfer every
        # few tens of megabytes, a large source then never finishes at all: the
        # attempts run out before any single one gets to the end. A part file is
        # still never mistaken for a finished download; `_download_candidates`
        # excludes `.part` outright.
        "continuedl": True,
        "file_access_retries": 20,
        "fragment_retries": 10,
        "retries": 10,
        # Fetch the stream in ranged chunks rather than one long request.
        # googlevideo hands out 403 partway through a single large transfer
        # from this network; each chunk is its own request, so a rejection
        # costs one chunk that yt-dlp then retries, instead of the whole file.
        "http_chunk_size": 10 * 1024 * 1024,
        "socket_timeout": get_settings().youtube.socket_timeout,
    }
    if on_progress is not None:
        opts["progress_hooks"] = [_progress_hook(on_progress)]
    try:
        apply_ytdlp_auth_options(preflight_opts)
        with yt_dlp.YoutubeDL(preflight_opts) as ydl:
            preflight_info = ydl.extract_info(source_ref, download=False)
        assert_downloadable_formats(preflight_info, source_ref)

        apply_ytdlp_auth_options(opts)
        # Logged once per download: when YouTube starts refusing, the first
        # question is always which cookies, runtime and client were in play.
        log.info(
            "yt-dlp: cookies=%s js_runtime=%s clients=%s",
            opts.get("cookiefile"),
            (opts.get("js_runtimes") or {}).keys(),
            (opts.get("extractor_args") or {}).get("youtube", {}).get("player_client"),
        )
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(source_ref, download=True)
    except Exception as exc:
        recovered_path = _recover_downloaded_file(originals_dir, job_id)
        if recovered_path:
            return recovered_path, preflight_info if "preflight_info" in locals() else {}
        raise RuntimeError(explain_ytdlp_auth_error(exc)) from exc

    existing = _download_candidates(info, originals_dir, job_id)
    if not existing:
        raise FileNotFoundError("yt-dlp finished but no downloaded file was found")
    return os.path.abspath(existing[0]), info


def _progress_hook(on_progress: ProgressCallback) -> Any:
    """Turn yt-dlp's per-chunk callback into an occasional fraction.

    yt-dlp fires this several times a second, and every report the caller makes
    costs a database round trip. Reporting on a timer instead keeps a slow
    download visibly moving without the progress writes outweighing the
    download itself.
    """
    state = {"at": 0.0}

    def hook(status: dict) -> None:
        if status.get("status") == "finished":
            on_progress(1.0)
            return
        if status.get("status") != "downloading":
            return
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        done = status.get("downloaded_bytes")
        if not total or done is None:
            return
        now = monotonic()
        if now - state["at"] < _PROGRESS_INTERVAL_SECONDS:
            return
        state["at"] = now
        on_progress(max(0.0, min(1.0, done / total)))

    return hook


def _download_candidates(info: dict[str, Any], originals_dir: str, job_id: int) -> list[str]:
    candidates: list[str] = []
    for requested in info.get("requested_downloads") or []:
        path = requested.get("filepath")
        if path:
            candidates.append(path)
    for key in ("filepath", "_filename", "filename"):
        path = info.get(key)
        if path:
            candidates.append(path)
    candidates.extend(str(path) for path in Path(originals_dir).glob(f"job-{job_id}.*"))
    existing = [
        path
        for path in candidates
        if path and os.path.exists(path) and not path.endswith((".part", ".ytdl"))
    ]
    unique = list(dict.fromkeys(existing))
    unique.sort(key=lambda path: (_is_format_intermediate(path, job_id), -os.path.getmtime(path)))
    return unique


def _recover_downloaded_file(originals_dir: str, job_id: int) -> str | None:
    # Only fully written files count. Promoting a .part leftover to the final
    # name would poison the cache: every retry then reuses the truncated file.
    #
    # A format intermediate (`job-7.f399.mp4`) is written in full and is still
    # not the source. yt-dlp fetches video and audio as separate files and
    # merges them last, so an interruption before the merge leaves a complete
    # file carrying only a video stream. Recovering that hands the rest of the
    # pipeline a silent video: transcription then dies inside PyAV with
    # "IndexError: tuple index out of range", hours later and nowhere near the
    # cause. Leave it for the next attempt to resume and merge — which it can,
    # because `continuedl` is on.
    candidates = [
        path
        for path in _download_candidates({}, originals_dir, job_id)
        if not _is_format_intermediate(path, job_id)
    ]
    if candidates:
        return os.path.abspath(candidates[0])
    return None


def _is_format_intermediate(path: str, job_id: int) -> bool:
    return bool(re.match(rf"^job-{job_id}\.f\d+\.", Path(path).name))


def _cached_metadata(source_ref: str, path: str) -> dict[str, Any]:
    """What can be known about an already-downloaded file without the network.

    Deliberately no title: the filename is `job-3.mp4`, and letting that stand
    in for the video's name puts "job-3 - часть 1" in the TikTok caption.
    Callers that need the real title fetch it — see `title_for_cached_source`.
    """
    return {
        "title": None,
        "duration": ffprobe_duration(path),
        "description": "",
        "chapters": [],
        "comments": [],
        "webpage_url": source_ref,
        "extractor": "cached",
    }


def title_for_cached_source(source_ref: str) -> dict[str, Any]:
    """Fetch just the metadata for a source whose file is already downloaded.

    One cheap request, no download. Worth it: without it a re-run of a job
    loses the video's name, and the name is the first line of every caption.
    """
    try:
        return extract_info(source_ref) or {}
    except Exception as exc:
        log.warning("could not refresh metadata for %s: %s", source_ref, exc)
        return {}
