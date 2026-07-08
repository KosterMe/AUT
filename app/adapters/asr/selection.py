from __future__ import annotations

import os
from typing import Any

from app.adapters.asr import cache as transcript_cache
from app.adapters.asr import whisper as transcription
from app.core.config import get_settings


def select_subtitle_transcript(
    original_path: str,
    *,
    fallback_segments: list[dict[str, Any]],
    start_sec: float,
    end_sec: float,
    enabled: bool,
    job_transcript_model: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    mode = render_transcript_mode()
    if not enabled:
        return fallback_segments, {
            "source": "disabled",
            "mode": mode,
            "model": job_transcript_model,
        }
    if mode == "job_transcript":
        return fallback_segments, {
            "source": "job_transcript",
            "mode": mode,
            "model": job_transcript_model,
        }

    # Smart default: the job transcript already carries word timestamps for
    # Whisper-sourced clips, so reuse them and skip a redundant Whisper pass.
    # Only fall through to (re)transcription when the window has no words
    # (e.g. YouTube captions), which is exactly what karaoke subtitles need.
    if mode == "auto" and _has_words_in_window(fallback_segments, start_sec, end_sec):
        return fallback_segments, {
            "source": "job_transcript_words",
            "mode": mode,
            "model": job_transcript_model,
            "word_timestamps": True,
        }

    return _cache_or_transcribe(
        original_path,
        fallback_segments=fallback_segments,
        start_sec=start_sec,
        end_sec=end_sec,
        mode=mode,
        job_transcript_model=job_transcript_model,
    )


def _cache_or_transcribe(
    original_path: str,
    *,
    fallback_segments: list[dict[str, Any]],
    start_sec: float,
    end_sec: float,
    mode: str,
    job_transcript_model: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if mode != "always_transcribe":
        cached = transcript_cache.load_render_transcript(
            original_path,
            start_sec=start_sec,
            end_sec=end_sec,
        )
        if cached:
            cached_segments, cache_meta = cached
            if cached_segments:
                return cached_segments, {
                    "source": "render_transcript_cache",
                    "mode": mode,
                    "model": cache_meta.get("settings"),
                    "segments": len(cached_segments),
                    "word_timestamps": True,
                    "cache_key": cache_meta.get("cache_key"),
                }

    try:
        fresh = transcription.transcribe_media_segment(
            original_path,
            start_sec=start_sec,
            end_sec=end_sec,
        )
        serialized = serialize_transcript_segments(fresh)
        if serialized:
            cache_meta = transcript_cache.save_render_transcript(
                original_path,
                start_sec=start_sec,
                end_sec=end_sec,
                segments=serialized,
            )
            return serialized, {
                "source": "render_retranscribe",
                "mode": mode,
                "model": transcription.transcription_settings(),
                "segments": len(serialized),
                "word_timestamps": True,
                "cache_key": cache_meta.get("cache_key"),
            }
    except Exception as exc:
        return fallback_segments, {
            "source": "job_transcript",
            "mode": mode,
            "fallback_reason": str(exc),
            "model": job_transcript_model,
        }
    return fallback_segments, {
        "source": "job_transcript",
        "mode": mode,
        "fallback_reason": "empty_render_transcript",
        "model": job_transcript_model,
    }


def _has_words_in_window(
    segments: list[Any],
    start_sec: float,
    end_sec: float,
    *,
    min_overlap: float = 0.02,
) -> bool:
    for segment in segments or []:
        words = segment.get("words") if isinstance(segment, dict) else getattr(segment, "words", None)
        for word in words or []:
            word_start = _num(word, "start_sec", "start")
            word_end = _num(word, "end_sec", "end")
            if word_start is None or word_end is None:
                continue
            if min(word_end, end_sec) - max(word_start, start_sec) > min_overlap:
                return True
    return False


def _num(item: Any, *keys: str) -> float | None:
    for key in keys:
        value = item.get(key) if isinstance(item, dict) else getattr(item, key, None)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def serialize_transcript_segments(
    segments: list[transcription.TranscriptSegment],
) -> list[dict[str, Any]]:
    return [
        {
            "start_sec": round(segment.start_sec, 3),
            "end_sec": round(segment.end_sec, 3),
            "text": segment.text[:700],
            "words": [
                {
                    "start_sec": round(word.start_sec, 3),
                    "end_sec": round(word.end_sec, 3),
                    "text": word.text[:80],
                }
                for word in (segment.words or [])[:120]
            ],
        }
        for segment in segments
    ]


def render_transcript_mode() -> str:
    """Where subtitle word timings come from at render time.

    The default reuses the job transcript's word timestamps when it has them
    and only re-transcribes the individual clip when they are missing — which
    is the case for caption sources that carry no word offsets.
    """
    return str(get_settings().render.transcript_mode)
