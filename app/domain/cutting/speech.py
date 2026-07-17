"""Cutting a video where the speaker stops talking.

The default cutter, and the only one with anything to say about *meaning*: it
grows a clip until it is long enough, then ends it at the first place a
sentence actually ends rather than at a fixed duration.

Pure logic — a transcript in, boundaries out. No database, no ffmpeg, no
filesystem, which is what makes the cutting rules, the part most likely to need
tuning, trivial to test and safe to change.
"""
from __future__ import annotations

import re
from dataclasses import replace

from app.domain.cutting.base import (
    DEFAULT_GAP_SECONDS,
    MIN_USABLE_SECONDS,
    SliceSpec,
    compact_text,
    title_from_text,
)
from app.domain.transcript import TranscriptSegment

# A pause this long is a sentence break regardless of punctuation — which
# matters because some engines punctuate poorly.
STRONG_PAUSE_SECONDS = 0.9
# A shorter pause still ends a clip once enough words have accumulated.
WEAK_PAUSE_SECONDS = 0.45
WORDS_FOR_WEAK_BOUNDARY = 18


def build_semantic_slices(
    segments: list[TranscriptSegment],
    *,
    source_duration: float,
    min_clip_seconds: float,
    max_clip_seconds: float,
    gap_seconds: float = DEFAULT_GAP_SECONDS,
    max_clips: int = 0,
) -> list[SliceSpec]:
    """Cut a transcript into clips that end on sentence boundaries.

    Coverage is complete: the first clip starts at 0, each next one starts
    `gap_seconds` before the previous ended, and the last is extended to the
    end of the source so an outro is never dropped.
    """
    normalized = _usable_segments(segments)
    if not normalized:
        return []

    specs: list[SliceSpec] = []
    index = 0
    previous_end: float | None = None
    cursor = 0

    while cursor < len(normalized):
        if max_clips and len(specs) >= max_clips:
            break
        # Skip segments already covered by the previous clip's overlap.
        while (
            cursor < len(normalized)
            and previous_end is not None
            and normalized[cursor].end_sec <= previous_end
        ):
            cursor += 1
        if cursor >= len(normalized):
            break

        start_sec = 0.0 if previous_end is None else max(0.0, previous_end - gap_seconds)
        end_index = _find_end(
            normalized,
            start_index=cursor,
            start_sec=start_sec,
            source_duration=source_duration,
            min_clip_seconds=min_clip_seconds,
            max_clip_seconds=max_clip_seconds,
        )

        end_sec = normalized[end_index].end_sec
        if source_duration > 0:
            end_sec = min(end_sec, source_duration)
        if end_sec - start_sec < MIN_USABLE_SECONDS:
            break

        chunk = normalized[cursor : end_index + 1]
        text = compact_text(" ".join(segment.text for segment in chunk))
        index += 1
        specs.append(
            SliceSpec(
                index=index,
                start_sec=round(start_sec, 3),
                end_sec=round(end_sec, 3),
                title=title_from_text(text, index),
                text=text,
                transcript_segments=len(chunk),
            )
        )
        previous_end = end_sec
        cursor = end_index + 1

    if specs and source_duration > 0 and specs[-1].end_sec < source_duration:
        specs[-1] = replace(specs[-1], end_sec=round(source_duration, 3))
    return specs


def is_sentence_boundary(text: str, pause_after: float) -> bool:
    """Whether a clip can end here without cutting somebody off mid-thought."""
    if pause_after >= STRONG_PAUSE_SECONDS:
        return True
    if text.rstrip().endswith((".", "!", "?", "...")):
        return True
    return pause_after >= WEAK_PAUSE_SECONDS and _word_count(text) >= WORDS_FOR_WEAK_BOUNDARY


def _usable_segments(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    return sorted(
        (s for s in segments if s.end_sec > s.start_sec and s.text.strip()),
        key=lambda segment: segment.start_sec,
    )


def _find_end(
    segments: list[TranscriptSegment],
    *,
    start_index: int,
    start_sec: float,
    source_duration: float,
    min_clip_seconds: float,
    max_clip_seconds: float,
) -> int:
    """Index of the segment this clip should end on."""
    last_index = start_index
    text_parts: list[str] = []

    for cursor in range(start_index, len(segments)):
        segment = segments[cursor]
        last_index = cursor
        text_parts.append(segment.text.strip())

        end_sec = min(segment.end_sec, source_duration) if source_duration > 0 else segment.end_sec
        duration = end_sec - start_sec
        text = compact_text(" ".join(text_parts))

        if duration >= min_clip_seconds and is_sentence_boundary(
            text, _pause_after(segments, cursor)
        ):
            return cursor
        if duration >= max_clip_seconds:
            # Past the limit with no clean break in sight: walk back to the
            # best boundary that still leaves a long enough clip.
            return _best_boundary_before(
                segments,
                start_index=start_index,
                end_index=cursor,
                start_sec=start_sec,
                min_clip_seconds=min_clip_seconds,
            )

    return last_index


def _best_boundary_before(
    segments: list[TranscriptSegment],
    *,
    start_index: int,
    end_index: int,
    start_sec: float,
    min_clip_seconds: float,
) -> int:
    for cursor in range(end_index, start_index - 1, -1):
        if segments[cursor].end_sec - start_sec < min_clip_seconds:
            continue
        text = compact_text(" ".join(s.text for s in segments[start_index : cursor + 1]))
        if is_sentence_boundary(text, _pause_after(segments, cursor)):
            return cursor
    return end_index


def _pause_after(segments: list[TranscriptSegment], index: int) -> float:
    """Silence after this segment. A large value when it is the last one."""
    if index + 1 >= len(segments):
        return 999.0
    return segments[index + 1].start_sec - segments[index].end_sec


def _word_count(text: str) -> int:
    return len(re.findall(r"\w+", text, flags=re.UNICODE))
