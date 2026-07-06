"""Transcript value objects.

These live in the domain, not next to a particular speech-to-text engine,
because everything downstream depends on them: slicing decides where to cut
from segment boundaries, subtitles time cues from word offsets, and captions
read the text. Whisper, NVIDIA Riva and YouTube captions are all just
different ways of producing this shape.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TranscriptWord:
    start_sec: float
    end_sec: float
    text: str


@dataclass(frozen=True)
class TranscriptSegment:
    start_sec: float
    end_sec: float
    text: str
    # None means the engine gave no word-level timing. Karaoke subtitles and
    # tight slice boundaries need words; captions-only sources often lack them.
    words: list[TranscriptWord] | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)

    @property
    def has_words(self) -> bool:
        return bool(self.words)


def serialize(segments: list[TranscriptSegment]) -> list[dict[str, Any]]:
    """Plain-dict form for caching and for storing in a task payload."""
    return [
        {
            "start_sec": round(segment.start_sec, 3),
            "end_sec": round(segment.end_sec, 3),
            "text": segment.text,
            "words": [
                {
                    "start_sec": round(word.start_sec, 3),
                    "end_sec": round(word.end_sec, 3),
                    "text": word.text,
                }
                for word in segment.words
            ]
            if segment.words
            else None,
        }
        for segment in segments
    ]


def deserialize(items: list[dict[str, Any]]) -> list[TranscriptSegment]:
    """Rebuild segments from cached dicts, dropping anything unusable.

    Accepts both `start_sec`/`end_sec` and bare `start`/`end` so transcripts
    cached by older versions still load instead of forcing a re-transcription.
    """
    segments: list[TranscriptSegment] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        start = _as_float(item.get("start_sec", item.get("start")))
        end = _as_float(item.get("end_sec", item.get("end")))
        text = str(item.get("text") or "").strip()
        if start is None or end is None or end <= start or not text:
            continue
        segments.append(
            TranscriptSegment(
                start_sec=start,
                end_sec=end,
                text=text,
                words=_deserialize_words(item.get("words")),
            )
        )
    return sorted(segments, key=lambda segment: segment.start_sec)


def words_in_window(
    segments: list[TranscriptSegment], start_sec: float, end_sec: float
) -> list[TranscriptWord]:
    """Every word that starts inside [start_sec, end_sec)."""
    return [
        word
        for segment in segments
        for word in (segment.words or [])
        if start_sec <= word.start_sec < end_sec
    ]


def _deserialize_words(raw: Any) -> list[TranscriptWord] | None:
    if not isinstance(raw, list):
        return None
    words: list[TranscriptWord] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start = _as_float(item.get("start_sec", item.get("start")))
        end = _as_float(item.get("end_sec", item.get("end")))
        text = str(item.get("text") or item.get("word") or "").strip()
        if start is None or end is None or not text:
            continue
        words.append(TranscriptWord(start_sec=start, end_sec=max(end, start), text=text))
    return words or None


def _as_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
