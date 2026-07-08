from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Any

from app.adapters.media import ffmpeg as media
from app.adapters.asr import whisper as transcription


CACHE_VERSION = 1


def load_media_transcript(
    source_path: str,
    *,
    settings: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    payload = _cache_payload("media_transcript", source_path, settings=settings)
    return _load_payload(payload)


def save_media_transcript(
    source_path: str,
    *,
    segments: list[dict[str, Any]],
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = _cache_payload("media_transcript", source_path, settings=settings)
    return _save_payload(payload, segments)


def load_render_transcript(
    source_path: str,
    *,
    start_sec: float,
    end_sec: float,
    settings: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    payload = _cache_payload(
        "render_transcript",
        source_path,
        start_sec=start_sec,
        end_sec=end_sec,
        settings=settings,
    )
    return _load_payload(payload)


def save_render_transcript(
    source_path: str,
    *,
    start_sec: float,
    end_sec: float,
    segments: list[dict[str, Any]],
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = _cache_payload(
        "render_transcript",
        source_path,
        start_sec=start_sec,
        end_sec=end_sec,
        settings=settings,
    )
    return _save_payload(payload, segments)


def render_transcript_cache_dir() -> str:
    path = os.path.join(media.media_root(), "transcripts")
    os.makedirs(path, exist_ok=True)
    return path


def _load_payload(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    path = _cache_path(payload["kind"], payload["cache_key"])
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None

    if data.get("cache_key") != payload["cache_key"]:
        return None
    segments = data.get("segments")
    if not isinstance(segments, list):
        return None
    return segments, {
        "cache_key": payload["cache_key"],
        "cache_path": path,
        "settings": data.get("settings") or payload["settings"],
        "source": data.get("source") or payload["source"],
        "window": data.get("window") or payload["window"],
    }


def _save_payload(payload: dict[str, Any], segments: list[dict[str, Any]]) -> dict[str, Any]:
    path = _cache_path(payload["kind"], payload["cache_key"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        **payload,
        "segments": segments,
    }
    fd, tmp_path = tempfile.mkstemp(
        prefix=".render-transcript-",
        suffix=".json",
        dir=os.path.dirname(path),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return {
        "cache_key": payload["cache_key"],
        "cache_path": path,
        "settings": payload["settings"],
        "source": payload["source"],
        "window": payload["window"],
    }


def _cache_path(kind: str, cache_key: str) -> str:
    prefix = "render" if kind == "render_transcript" else "media"
    return os.path.join(render_transcript_cache_dir(), f"{prefix}-{cache_key}.json")


def _cache_payload(
    kind: str,
    source_path: str,
    *,
    start_sec: float | None = None,
    end_sec: float | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = _source_signature(source_path)
    settings = settings or transcription.transcription_settings()
    window = None
    if start_sec is not None and end_sec is not None:
        window = {
            "start_sec": round(float(start_sec), 3),
            "end_sec": round(float(end_sec), 3),
        }
    hash_input = {
        "version": CACHE_VERSION,
        "kind": kind,
        "source": source,
        "settings": settings,
        "window": window,
    }
    cache_key = hashlib.sha256(
        json.dumps(hash_input, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:32]
    return {
        **hash_input,
        "cache_key": cache_key,
    }


def _source_signature(path: str) -> dict[str, Any]:
    absolute = os.path.abspath(path)
    try:
        stat = os.stat(absolute)
    except OSError:
        return {
            "path": absolute,
            "exists": False,
        }
    return {
        "path": absolute,
        "exists": True,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
