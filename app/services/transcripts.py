"""Getting a transcript for a source video.

Tries the cheapest source first and only pays for GPU/CPU time as a last
resort:

    cached transcript  ->  cached captions  ->  YouTube captions  ->  ASR

Transcribing an hour-long video is by far the most expensive step in the
pipeline, so the ordering here is what makes re-slicing a video with different
clip lengths nearly free.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from app.adapters.asr import cache as transcript_cache
from app.adapters.asr import service as asr
from app.adapters.asr import whisper
from app.adapters.youtube import transcripts as youtube_captions
from app.domain import transcript as transcript_model
from app.domain.transcript import TranscriptSegment

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str, float], None]


@dataclass(frozen=True)
class TranscriptResult:
    segments: list[TranscriptSegment]
    source: str
    settings: dict[str, Any]

    @property
    def has_word_timings(self) -> bool:
        return any(segment.has_words for segment in self.segments)


def load_or_build(
    media_path: str,
    *,
    source_ref: str,
    metadata: dict[str, Any],
    duration: float = 0.0,
    on_progress: ProgressCallback | None = None,
) -> TranscriptResult:
    report = on_progress or (lambda stage, value: None)
    asr_settings = whisper.transcription_settings()

    cached = transcript_cache.load_media_transcript(media_path)
    if cached:
        segments, meta = cached
        log.info("reusing cached transcript for %s", media_path)
        return TranscriptResult(transcript_model.deserialize(segments), "cache", meta)

    caption_settings = youtube_captions.cache_settings(source_ref)
    cached_captions = transcript_cache.load_media_transcript(media_path, settings=caption_settings)
    if cached_captions:
        segments, meta = cached_captions
        log.info("reusing cached YouTube captions for %s", media_path)
        return TranscriptResult(transcript_model.deserialize(segments), "captions_cache", meta)

    report("fetching_captions", 0.2)
    caption_transcript = youtube_captions.fetch_youtube_transcript(source_ref, metadata)
    if caption_transcript:
        serialized = transcript_model.serialize(caption_transcript.segments)
        meta = transcript_cache.save_media_transcript(
            media_path, segments=serialized, settings=caption_settings
        )
        log.info("using YouTube captions (%d segments)", len(caption_transcript.segments))
        return TranscriptResult(caption_transcript.segments, "youtube_captions", meta)

    report("transcribing", 0.25)
    log.info("transcribing %s (%.0fs) with the %s backend", media_path, duration, whisper.asr_backend())
    segments = asr.transcribe(media_path, duration=duration)
    serialized = transcript_model.serialize(segments)
    transcript_cache.save_media_transcript(media_path, segments=serialized)
    return TranscriptResult(segments, whisper.asr_backend(), asr_settings)
