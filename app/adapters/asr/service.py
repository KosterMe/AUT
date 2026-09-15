"""The speech-to-text entry point.

Callers ask for a transcript; this module decides how to get one. Today that
means local faster-whisper or hosted NVIDIA Riva, chosen by
`settings.asr.backend`, with chunk-parallel decoding for long media.

Having one entry point is what lets a new engine be added without touching
slicing, subtitles, or any handler.
"""
from __future__ import annotations

import logging
import os

from app.adapters.asr import whisper
from montage import client as montage
from app.core.errors import PermanentError
from app.domain.transcript import TranscriptSegment

log = logging.getLogger(__name__)


def transcribe(
    media_path: str,
    *,
    duration: float = 0.0,
    language: str | None = None,
) -> list[TranscriptSegment]:
    """Transcribe a whole file.

    Long media is split into overlapping chunks and decoded in parallel; each
    segment is owned by the chunk its start time falls in, so the overlap adds
    context without duplicating words at the seams.
    """
    _require_audio(media_path)
    return whisper.transcribe_media_chunked(media_path, duration=duration, language=language)


def _require_audio(media_path: str) -> None:
    """Refuse a file with no sound, in words.

    PyAV reaches for the first audio stream of whatever it is handed, so a
    silent video fails as `IndexError: tuple index out of range` from inside
    the decoder — a sentence that names neither the file nor the problem. The
    usual way to get one here is an interrupted download whose video and audio
    streams were never merged.
    """
    if montage.has_audio(media_path):
        return
    raise PermanentError(
        f"{os.path.basename(media_path)} has no audio track, so there is nothing "
        "to transcribe. A video-only file usually means a download was cut short "
        "before yt-dlp merged its separate video and audio streams; delete it and "
        "let the job fetch the source again."
    )


def transcribe_segment(
    media_path: str,
    *,
    start_sec: float,
    end_sec: float,
    language: str | None = None,
) -> list[TranscriptSegment]:
    """Transcribe one window, with timings returned in source-relative seconds.

    Used when a clip needs word-level timing that the whole-file transcript
    does not carry — caption sources, for instance, have no word offsets.
    """
    return whisper.transcribe_media_segment(
        media_path, start_sec=start_sec, end_sec=end_sec, language=language
    )


def settings_fingerprint() -> dict:
    """The settings that affect transcript text, used as a cache key.

    Performance knobs are deliberately excluded: changing thread count must
    not invalidate a cached transcript.
    """
    return whisper.transcription_settings()
