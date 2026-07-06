from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.adapters.youtube import downloader
from app.domain import sources
from app.adapters.asr import whisper as transcription
from app.core.config import get_settings


# Part of the caption cache key. Bumped to 2 when json3 parsing started
# keeping word timings, so transcripts cached without them are re-fetched
# rather than quietly forcing a re-transcription at render time.
PARSER_VERSION = 2
# Floor for a word's on-screen life when the next offset is not usable.
MIN_WORD_MS = 120.0
_PARSEABLE_EXTS = {"json3", "vtt"}
_FORMAT_PRIORITY = {"json3": 0, "vtt": 1}


@dataclass(frozen=True)
class CaptionTranscript:
    segments: list[transcription.TranscriptSegment]
    meta: dict[str, Any]


def enabled() -> bool:
    return get_settings().asr.use_youtube_captions


def cache_settings(source_ref: str) -> dict[str, Any]:
    return {
        "engine": "youtube_captions",
        "source_ref": source_ref,
        "preferred_languages": preferred_languages(),
        "parser_version": PARSER_VERSION,
    }


def preferred_languages() -> list[str]:
    values = [lang.strip().lower() for lang in get_settings().asr.youtube_caption_langs]
    return [value for value in values if value] or ["*"]


def fetch_youtube_transcript(
    source_ref: str,
    metadata: dict[str, Any],
) -> CaptionTranscript | None:
    if not enabled() or sources.infer_platform(source_ref) != "youtube":
        return None

    infos = [metadata]
    if not _has_caption_tracks(metadata):
        try:
            infos.append(downloader.extract_info(source_ref, get_comments=False))
        except Exception as exc:
            # Swallowed, because a video may genuinely have no captions and ASR
            # is the answer then. But say so: silently, this is indistinguishable
            # from "no captions exist", and the difference is whether the next
            # step is a three-gigabyte model download that was never needed.
            log.warning(
                "could not look up caption tracks for %s, falling back to ASR: %s",
                source_ref,
                exc,
            )

    errors: list[str] = []
    for info in infos:
        selected = select_caption_track(info)
        if not selected:
            continue
        try:
            text = _download_caption(selected["url"])
            segments = parse_caption_payload(text, selected["ext"])
            if segments:
                return CaptionTranscript(
                    segments=segments,
                    meta={
                        "source": "youtube_captions",
                        "kind": selected["kind"],
                        "language": selected["language"],
                        "ext": selected["ext"],
                        "name": selected.get("name"),
                        "segments": len(segments),
                    },
                )
        except Exception as exc:
            errors.append(str(exc))
    return None


def select_caption_track(metadata: dict[str, Any]) -> dict[str, Any] | None:
    tracks: list[dict[str, Any]] = []
    for kind, field, kind_priority in (
        ("manual", "subtitles", 0),
        ("automatic", "automatic_captions", 1),
    ):
        entries_by_lang = metadata.get(field)
        if not isinstance(entries_by_lang, dict):
            continue
        for language, entries in entries_by_lang.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                ext = str(entry.get("ext") or "").lower()
                url = str(entry.get("url") or "")
                if ext not in _PARSEABLE_EXTS or not url:
                    continue
                tracks.append(
                    {
                        "kind": kind,
                        "kind_priority": kind_priority,
                        "language": str(language),
                        "language_priority": _language_priority(str(language)),
                        "ext": ext,
                        "format_priority": _FORMAT_PRIORITY.get(ext, 99),
                        "url": url,
                        "name": entry.get("name"),
                    }
                )
    if not tracks:
        return None
    return sorted(
        tracks,
        key=lambda item: (
            item["kind_priority"],
            item["language_priority"],
            item["format_priority"],
        ),
    )[0]


def parse_caption_payload(payload: str, ext: str) -> list[transcription.TranscriptSegment]:
    ext = ext.lower()
    if ext == "json3":
        return parse_json3(payload)
    if ext == "vtt":
        return parse_vtt(payload)
    return []


def parse_json3(payload: str) -> list[transcription.TranscriptSegment]:
    """Parse YouTube's json3 caption format, keeping word-level timing.

    Auto-generated captions carry a `tOffsetMs` on each piece of a cue, which
    is word timing in all but name. Extracting it is what lets karaoke
    subtitles be built straight from captions — without it, every clip has to
    be re-transcribed at render time just to recover the timings that were
    sitting in this payload all along.

    Manually uploaded captions have one piece per cue and no offsets; those
    yield no words, and the render falls back to transcribing.
    """
    data = json.loads(payload)
    events = data.get("events") or []
    segments: list[transcription.TranscriptSegment] = []

    for event in events:
        if not isinstance(event, dict) or "segs" not in event:
            continue
        start_ms = _as_float(event.get("tStartMs"))
        if start_ms is None:
            continue
        segs = event.get("segs") or []
        duration_ms = _as_float(event.get("dDurationMs")) or 1500.0
        text = _clean_text("".join(str(seg.get("utf8") or "") for seg in segs))
        if not text:
            continue
        start = round(start_ms / 1000.0, 3)
        end = round((start_ms + max(duration_ms, 200.0)) / 1000.0, 3)
        if end <= start:
            continue
        segments.append(
            transcription.TranscriptSegment(
                start_sec=start,
                end_sec=end,
                text=text,
                words=_json3_words(segs, event_start_ms=start_ms, event_end_ms=end * 1000.0),
            )
        )
    return _dedupe_segments(segments)


def _json3_words(
    segs: list[Any], *, event_start_ms: float, event_end_ms: float
) -> list[transcription.TranscriptWord] | None:
    """Turn one cue's pieces into words. None when there is no timing to take."""
    pieces: list[tuple[float | None, str]] = []
    for seg in segs:
        if not isinstance(seg, dict):
            continue
        raw = str(seg.get("utf8") or "")
        # Whitespace-only pieces are separators between words, not words.
        if not raw.strip():
            continue
        pieces.append((_as_float(seg.get("tOffsetMs")), raw.strip()))

    if not pieces:
        return None
    # No offsets anywhere: a manually uploaded caption. Nothing word-level here.
    if all(offset is None for offset, _ in pieces):
        return None

    # The first piece usually omits tOffsetMs, meaning "at the cue's start".
    starts: list[float] = []
    running = 0.0
    for offset, _ in pieces:
        if offset is not None:
            running = offset
        starts.append(event_start_ms + running)

    # YouTube repeats an offset when two pieces were emitted together, which
    # would give a word zero length and make it overlap its neighbour. Spacing
    # them out keeps cues strictly in order — subtitles that overlap render on
    # top of each other.
    for index in range(1, len(starts)):
        starts[index] = max(starts[index], starts[index - 1] + MIN_WORD_MS)

    words: list[transcription.TranscriptWord] = []
    for index, (start_ms, (_, text)) in enumerate(zip(starts, pieces)):
        # A word runs until the next one starts, or to the end of the cue.
        if index + 1 < len(starts):
            end_ms = starts[index + 1]
        else:
            end_ms = max(event_end_ms, start_ms + MIN_WORD_MS)
        words.append(
            transcription.TranscriptWord(
                start_sec=round(start_ms / 1000.0, 3),
                end_sec=round(end_ms / 1000.0, 3),
                text=text,
            )
        )
    return words or None


def parse_vtt(payload: str) -> list[transcription.TranscriptSegment]:
    lines = payload.replace("\ufeff", "").splitlines()
    segments: list[transcription.TranscriptSegment] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or line == "WEBVTT" or line.startswith(("NOTE", "STYLE", "REGION")):
            index += 1
            continue
        if "-->" not in line and index + 1 < len(lines) and "-->" in lines[index + 1]:
            index += 1
            line = lines[index].strip()
        if "-->" not in line:
            index += 1
            continue
        start, end = _parse_time_range(line)
        index += 1
        text_lines: list[str] = []
        while index < len(lines) and lines[index].strip():
            text_lines.append(lines[index].strip())
            index += 1
        text = _clean_text(" ".join(text_lines))
        if start is not None and end is not None and end > start and text:
            segments.append(
                transcription.TranscriptSegment(
                    start_sec=round(start, 3),
                    end_sec=round(end, 3),
                    text=text,
                )
            )
    return _dedupe_segments(segments)


def _download_caption(url: str) -> str:
    timeout = _env_float("AUTOCLIPS_YOUTUBE_CAPTION_TIMEOUT_SECONDS", 20.0)
    use_env_proxy = _env_bool("AUTOCLIPS_YOUTUBE_CAPTION_USE_ENV_PROXY", default=False)
    with httpx.Client(timeout=timeout, trust_env=use_env_proxy, follow_redirects=True) as client:
        response = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        return response.text


def _has_caption_tracks(metadata: dict[str, Any]) -> bool:
    return bool(metadata.get("subtitles") or metadata.get("automatic_captions"))


def _language_priority(language: str) -> int:
    lang = language.lower()
    preferences = preferred_languages()
    if lang in preferences:
        return preferences.index(lang)
    for index, pref in enumerate(preferences):
        if pref != "*" and lang.startswith(f"{pref}-"):
            return index + 20
    if "*" in preferences:
        return preferences.index("*") + 100
    return 999


def _parse_time_range(line: str) -> tuple[float | None, float | None]:
    left, _, right = line.partition("-->")
    return _parse_timestamp(left.strip()), _parse_timestamp(right.strip().split()[0])


def _parse_timestamp(value: str) -> float | None:
    value = value.replace(",", ".")
    parts = value.split(":")
    try:
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
            return hours * 3600 + minutes * 60 + seconds
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = float(parts[1])
            return minutes * 60 + seconds
    except ValueError:
        return None
    return None


def _clean_text(text: str) -> str:
    text = re.sub(r"<\d{1,2}:\d{2}(?::\d{2})?\.\d{3}>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def _dedupe_segments(
    segments: list[transcription.TranscriptSegment],
) -> list[transcription.TranscriptSegment]:
    result: list[transcription.TranscriptSegment] = []
    for segment in sorted(segments, key=lambda item: item.start_sec):
        if result and segment.text == result[-1].text and segment.start_sec <= result[-1].end_sec + 0.2:
            previous = result.pop()
            result.append(
                transcription.TranscriptSegment(
                    start_sec=previous.start_sec,
                    end_sec=max(previous.end_sec, segment.end_sec),
                    text=previous.text,
                    # Keep whichever copy carries word timings; dropping them
                    # here would silently undo the json3 parsing.
                    words=previous.words or segment.words,
                )
            )
            continue
        result.append(segment)
    return result


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
