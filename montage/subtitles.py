from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from montage.config import get_settings
from montage.style import SubtitleStyle

# Word-level ("karaoke") subtitles: each cue shows exactly one word and appears
# exactly at that word's start time. Nothing is ever glued together — a short
# word getting a short cue is the point of the look. The legacy segment/sentence
# machinery was removed in favour of this path; a compact fallback still handles
# transcripts that arrive without word timestamps (e.g. YouTube captions when
# the render retranscribe pass is disabled or fails).

BASE_TAGS = r"\blur0.2"
# Fallback line length. The style carries the real one; this is what a caller
# that has no style gets.
MAX_LINE_CHARS = 24

# Cue entrance: the word lands slightly small, overshoots, then settles. Kept
# under ~0.17s on purpose — cues are short, and a slower move reads as lag.
POP_START_SCALE = 88
POP_PEAK_SCALE = 104
POP_IN_MS = 90
POP_SETTLE_MS = 80
FADE_IN_MS = 70

# Shortest cue that still gets written. Two frames at 30fps: with nothing glued
# together any more, this floor is the only thing that can drop a word, and a
# word that flashes is better than a word that is missing.
DEFAULT_ASS_MIN_SECONDS = 0.06

# Display font for the headline/cover. Bundled with the repo (see assets/fonts)
# and loaded via libass fontsdir, so it renders the same on Windows and Linux.
DEFAULT_TITLE_FONT = "Oswald"


def title_font_name() -> str:
    return get_settings().subtitles.title_font.strip() or DEFAULT_TITLE_FONT


def ass_colour(value: str, opacity: float = 1.0) -> str:
    """`#RRGGBB` as libass wants it: `&HAABBGGRR`, alpha inverted.

    Inverted because in ASS 00 is opaque and FF is invisible, which is exactly
    backwards from every colour picker a person will paste a value out of. An
    unparseable colour falls back to white rather than raising: a typo in a
    preset should cost the colour, not the clip.
    """
    text = (value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6 or any(char not in "0123456789abcdefABCDEF" for char in text):
        text = "FFFFFF"
    alpha = int(round((1.0 - max(0.0, min(1.0, opacity))) * 255))
    return f"&H{alpha:02X}{text[4:6]}{text[2:4]}{text[0:2]}".upper()


# Fallback (no word timestamps) tuning.
FALLBACK_MAX_SECONDS = 2.8
FALLBACK_TEXT_CHUNK_WORDS = 6

_TRAIL_PUNCT = ".,!?;:…\"'»«)]}"
_LEAD_PUNCT = "\"'«([{-–—"

STAGE_DIRECTIONS = {
    "аплодисменты",
    "звонок",
    "крик",
    "музыка",
    "смех",
    "стон",
    "шум",
}


@dataclass(frozen=True)
class SubtitleCue:
    start_sec: float
    end_sec: float
    text: str


@dataclass(frozen=True)
class TimelineSegment:
    source_start_sec: float
    source_end_sec: float
    output_start_sec: float
    output_end_sec: float


@dataclass(frozen=True)
class CueOptions:
    hold_seconds: float = 0.15      # extra on-screen time when a pause follows
    end_hold_seconds: float = 0.35
    min_cue_seconds: float = DEFAULT_ASS_MIN_SECONDS
    strip_punctuation: bool = True
    uppercase: bool = False
    max_line_chars: int = MAX_LINE_CHARS
    time_offset_seconds: float = 0.0  # shift all cues; negative shows them earlier

    @classmethod
    def from_settings(cls) -> "CueOptions":
        configured = get_settings().subtitles
        return cls(
            strip_punctuation=configured.strip_punct,
            uppercase=configured.uppercase,
            time_offset_seconds=configured.time_offset_seconds,
        )

    @classmethod
    def from_style(cls, style: SubtitleStyle) -> "CueOptions":
        """Cue timing and casing as the clip's own style asks for it.

        The same numbers the environment used to supply, except that these
        arrived with the clip — so two jobs rendering at the same moment can
        want different things.
        """
        return cls(
            hold_seconds=style.hold_seconds,
            end_hold_seconds=style.end_hold_seconds,
            strip_punctuation=style.strip_punctuation,
            uppercase=style.uppercase,
            time_offset_seconds=style.time_offset_seconds,
            max_line_chars=style.max_line_chars,
        )


def make_timeline_segments(
    clip_start_sec: float,
    keep_segments: list[tuple[float, float]],
) -> list[TimelineSegment]:
    timeline: list[TimelineSegment] = []
    output_cursor = 0.0
    for rel_start, rel_end in keep_segments:
        rel_start = max(0.0, float(rel_start))
        rel_end = max(rel_start, float(rel_end))
        if rel_end - rel_start < 0.05:
            continue
        source_start = clip_start_sec + rel_start
        source_end = clip_start_sec + rel_end
        output_end = output_cursor + (source_end - source_start)
        timeline.append(
            TimelineSegment(
                source_start_sec=round(source_start, 3),
                source_end_sec=round(source_end, 3),
                output_start_sec=round(output_cursor, 3),
                output_end_sec=round(output_end, 3),
            )
        )
        output_cursor = output_end
    return timeline


def make_subtitle_cues(
    transcript_segments: list[Any],
    *,
    timeline_segments: list[TimelineSegment],
    fallback_text: str | None = None,
    options: CueOptions | None = None,
    style: SubtitleStyle | None = None,
) -> list[SubtitleCue]:
    opts = options or (CueOptions.from_style(style) if style else CueOptions.from_settings())
    normalized = _coerce_segments(transcript_segments)

    words = _mapped_words(normalized, timeline_segments)
    if words:
        cues = _tight_cues(words, timeline_segments, opts)
        if cues:
            return _shift_cues(cues, opts)

    cues = _fallback_cues_from_segments(normalized, timeline_segments, opts)
    if cues:
        return _shift_cues(cues, opts)

    if fallback_text:
        output_duration = timeline_segments[-1].output_end_sec if timeline_segments else 0.0
        if output_duration > 0:
            return _shift_cues(_fallback_cues_from_text(fallback_text, output_duration, opts), opts)
    return []


def _shift_cues(cues: list[SubtitleCue], opts: CueOptions) -> list[SubtitleCue]:
    offset = opts.time_offset_seconds
    if not offset:
        return cues
    shifted: list[SubtitleCue] = []
    for cue in cues:
        start = max(0.0, cue.start_sec + offset)
        end = max(start + opts.min_cue_seconds, cue.end_sec + offset)
        shifted.append(SubtitleCue(start_sec=round(start, 3), end_sec=round(end, 3), text=cue.text))
    return shifted


def _cue_tags(duration_sec: float, *, animate: bool) -> str:
    """Override tags in front of a cue's text.

    The animation is a pop: the word appears at `POP_START_SCALE`, overshoots
    slightly and settles at 100%, with a quick fade-in. There is no fade-out —
    cues run back to back, and fading one word away before the next arrives
    reads as a flicker.

    Every duration is measured against the cue itself and held to roughly half
    its length, so a 0.13s word is not still growing when it disappears.
    """
    if not animate:
        return "{" + BASE_TAGS + "}"
    duration_ms = max(1.0, duration_sec * 1000.0)
    span = min(POP_IN_MS + POP_SETTLE_MS, duration_ms * 0.5)
    grow = max(20, int(span * POP_IN_MS / (POP_IN_MS + POP_SETTLE_MS)))
    settle = max(20, int(span) - grow)
    fade_in = max(1, min(FADE_IN_MS, int(duration_ms * 0.25)))
    return (
        "{" + BASE_TAGS
        + f"\\fad({fade_in},0)"
        + f"\\fscx{POP_START_SCALE}\\fscy{POP_START_SCALE}"
        + f"\\t(0,{grow},\\fscx{POP_PEAK_SCALE}\\fscy{POP_PEAK_SCALE})"
        + f"\\t({grow},{grow + settle},\\fscx100\\fscy100)"
        + "}"
    )


def write_ass_file(
    cues: list[SubtitleCue],
    output_path: str,
    *,
    width: int,
    height: int,
    style: SubtitleStyle | None = None,
    min_cue_seconds: float = DEFAULT_ASS_MIN_SECONDS,
    title_text: str | None = None,
    title_end_sec: float = 0.0,
) -> str:
    """Write the burned overlay: one file for the karaoke cues and the headline.

    Everything visual comes from `style` — the font included. It used to be
    Arial, hardcoded, while the headline two lines below rendered in the
    bundled display face; the mismatch was the most visible thing about a
    finished clip and could not be changed without editing this function.
    """
    look = style or SubtitleStyle.from_settings()
    animate = look.animate
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    font_size = int(look.font_size)
    margin_v = max(70, int(height * (100 - look.position_percent) / 100))
    outline = max(1, int(font_size * look.outline_ratio))
    shadow = max(0, int(font_size * look.shadow_ratio))
    primary = ass_colour(look.colour)
    border = ass_colour(look.outline_colour)
    back = ass_colour(look.outline_colour, look.shadow_opacity)
    styles = [
        (
            f"Style: Default,{look.font or DEFAULT_TITLE_FONT},"
            f"{font_size},{primary},&H000000FF,{border},{back},"
            f"-1,0,0,0,100,100,0,0,1,{outline},{shadow},2,78,78,{margin_v},1"
        )
    ]
    events: list[str] = []
    for cue in cues:
        duration = cue.end_sec - cue.start_sec
        if duration < min_cue_seconds:
            continue
        events.append(
            "Dialogue: 0,"
            f"{_ass_time(cue.start_sec)},{_ass_time(cue.end_sec)},"
            f"Default,,0,0,0,,{_cue_tags(duration, animate=animate)}{_ass_text(cue.text)}"
        )

    title = _clean_text(title_text or "")
    if title:
        styles.append(_title_banner_style(width, height))
        end = title_end_sec if title_end_sec > 0 else 359999.0
        events.append(
            "Dialogue: 0,"
            f"{_ass_time(0.0)},{_ass_time(end)},"
            f"Top,,0,0,0,,{_ass_text(_wrap_title(title))}"
        )

    content = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.601",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        *styles,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *events,
    ]
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(content) + "\n")
    return output_path


def write_cover_ass_file(
    output_path: str,
    *,
    width: int,
    height: int,
    title_text: str | None = None,
    part_text: str | None = None,
) -> str:
    """Static overlay for a clip cover: the source title up top and a big part badge."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    font = title_font_name()
    title_font = max(66, int(height * 0.05))
    part_font = max(120, int(height * 0.12))
    top_margin = max(150, int(height * 0.12))
    styles = [
        # Title near the top — no background slab, heavy outline (BorderStyle 1).
        (
            f"Style: CoverTitle,{font},"
            f"{title_font},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
            f"-1,0,0,0,100,100,1,0,1,{max(6, int(title_font * 0.11))},"
            f"{max(2, int(title_font * 0.04))},8,50,50,{top_margin},1"
        ),
        # Oversized centered "ЧАСТЬ N" badge.
        (
            f"Style: CoverPart,{font},"
            f"{part_font},&H0000E5FF,&H000000FF,&H00000000,&HA0000000,"
            f"-1,0,0,0,100,100,2,0,1,{max(10, int(part_font * 0.10))},"
            f"{max(3, int(part_font * 0.04))},5,60,60,0,1"
        ),
    ]
    events: list[str] = []
    title = _clean_text(title_text or "")
    if title:
        events.append(
            "Dialogue: 0,0:00:00.00,9:59:59.00,CoverTitle,,0,0,0,,"
            f"{_ass_text(_wrap_title(title))}"
        )
    part = _clean_text(part_text or "")
    if part:
        events.append(
            "Dialogue: 0,0:00:00.00,9:59:59.00,CoverPart,,0,0,0,,"
            f"{_ass_text(part.upper())}"
        )

    content = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.601",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        *styles,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *events,
    ]
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(content) + "\n")
    return output_path


def _title_banner_style(width: int, height: int) -> str:
    # Persistent headline pinned near the top, dropped a bit below the very edge
    # so it clears the platform UI and reads as a deliberate hook. No background
    # slab — big white letters in the bundled display font with a heavy black
    # outline and shadow (BorderStyle 1) so it stays legible over any footage.
    font_size = max(66, int(height * 0.049))
    outline = max(6, int(font_size * 0.11))
    shadow = max(2, int(font_size * 0.04))
    top_margin = max(150, int(height * 0.12))
    side_margin = max(40, int(width * 0.04))
    return (
        f"Style: Top,{title_font_name()},"
        f"{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
        f"-1,0,0,0,100,100,1,0,1,{outline},{shadow},8,{side_margin},{side_margin},{top_margin},1"
    )


def _wrap_title(text: str, *, line_limit: int = 26, max_lines: int = 2) -> str:
    text = _clean_text(text)
    if len(text) <= line_limit:
        return text
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        added = len(word) + (1 if current else 0)
        if current and current_len + added > line_limit and len(lines) < max_lines - 1:
            lines.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += added
    if current:
        lines.append(" ".join(current))
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    # If the title still overflows the last allowed line, clip it with an ellipsis.
    if len(lines) == max_lines and len(lines[-1]) > line_limit:
        lines[-1] = lines[-1][: line_limit - 1].rstrip() + "…"
    return "\\N".join(lines)


# --- word path -------------------------------------------------------------


def _mapped_words(
    segments: list[dict[str, Any]],
    timeline_segments: list[TimelineSegment],
) -> list[dict[str, Any]]:
    """Project every transcript word onto the output timeline.

    A word that straddles an auto-montage cut is split into fragments, one per
    kept timeline window. Timing is preserved exactly (no rounding of onsets).
    """
    mapped: list[dict[str, Any]] = []
    for segment in segments:
        for word in segment.get("words") or []:
            raw = str(word.get("text") or "").strip()
            start = _as_float(word.get("start_sec"))
            end = _as_float(word.get("end_sec"))
            if not raw or start is None or end is None or end <= start:
                continue
            for piece_start, piece_end in _project(start, end, timeline_segments, min_overlap=0.04):
                mapped.append({"start_sec": piece_start, "end_sec": piece_end, "raw": raw})
    return sorted(mapped, key=lambda item: item["start_sec"])


def _tight_cues(
    words: list[dict[str, Any]],
    timeline_segments: list[TimelineSegment],
    opts: CueOptions,
) -> list[SubtitleCue]:
    """One cue per word, each starting on its own onset and ending at the next.

    A cue lasts until the following word begins (plus a short hold when a pause
    follows), so the screen never shows two words at once and never shows one
    word standing in for two.
    """
    window_ends = {round(seg.output_end_sec, 3) for seg in timeline_segments}
    cues: list[SubtitleCue] = []
    for index, word in enumerate(words):
        start = word["start_sec"]
        word_end = word["end_sec"]
        if index + 1 < len(words):
            next_start = words[index + 1]["start_sec"]
            end = min(word_end + opts.hold_seconds, next_start)
            # A montage cut between this word and the next: do not let the cue
            # bleed across the boundary.
            crossing = min((edge for edge in window_ends if start < edge <= next_start), default=None)
            if crossing is not None:
                end = min(end, crossing)
            end = max(end, min(start + opts.min_cue_seconds, next_start))
        else:
            end = word_end + opts.end_hold_seconds
        text = _display_word(word["raw"], opts)
        if text and end - start >= opts.min_cue_seconds:
            cues.append(SubtitleCue(start_sec=round(start, 3), end_sec=round(end, 3), text=text))
    return cues


# --- fallback path (no word timestamps) ------------------------------------


def _fallback_cues_from_segments(
    segments: list[dict[str, Any]],
    timeline_segments: list[TimelineSegment],
    opts: CueOptions,
) -> list[SubtitleCue]:
    """One caption card per (segment ∩ kept-window), capped in length.

    Used only when the transcript lacks word timestamps (e.g. YouTube captions
    without a render retranscribe pass). Rolling/overlapping caption lines are
    trimmed first so they don't stack on screen.
    """
    cues: list[SubtitleCue] = []
    for segment in _cap_overlaps(segments):
        text = _display_card(segment["text"], opts)
        if not text:
            continue
        for out_start, out_end in _project(
            segment["start_sec"], segment["end_sec"], timeline_segments, min_overlap=0.35
        ):
            end = min(out_end, out_start + FALLBACK_MAX_SECONDS)
            if end - out_start >= opts.min_cue_seconds:
                cues.append(SubtitleCue(start_sec=round(out_start, 3), end_sec=round(end, 3), text=text))
    return _dedupe_non_overlap(cues, opts)


def _fallback_cues_from_text(text: str, output_duration: float, opts: CueOptions) -> list[SubtitleCue]:
    words = [word for word in _clean_text(text).split() if word]
    if not words:
        return []
    chunks = [
        words[i : i + FALLBACK_TEXT_CHUNK_WORDS]
        for i in range(0, len(words), FALLBACK_TEXT_CHUNK_WORDS)
    ]
    duration = max(0.1, output_duration)
    per_chunk = duration / len(chunks)
    cues: list[SubtitleCue] = []
    cursor = 0.0
    for chunk in chunks:
        end = min(output_duration, cursor + min(per_chunk, FALLBACK_MAX_SECONDS))
        card = _display_card(" ".join(chunk), opts)
        if card and end - cursor >= opts.min_cue_seconds:
            cues.append(SubtitleCue(start_sec=round(cursor, 3), end_sec=round(end, 3), text=card))
        cursor += per_chunk
    return _dedupe_non_overlap(cues, opts)


def _cap_overlaps(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim overlapping caption segments so rolling YouTube captions don't stack."""
    ordered = sorted(segments, key=lambda item: item["start_sec"])
    result: list[dict[str, Any]] = []
    for index, segment in enumerate(ordered):
        item = {**segment}
        for nxt in ordered[index + 1 :]:
            next_start = float(nxt["start_sec"])
            if next_start <= item["start_sec"]:
                continue
            if next_start < item["end_sec"]:
                item["end_sec"] = next_start
            break
        if item["end_sec"] - item["start_sec"] >= 0.2:
            result.append(item)
    return result


def _dedupe_non_overlap(cues: list[SubtitleCue], opts: CueOptions) -> list[SubtitleCue]:
    ordered = sorted(cues, key=lambda cue: (cue.start_sec, cue.end_sec))
    result: list[SubtitleCue] = []
    for cue in ordered:
        start = cue.start_sec
        if result and start < result[-1].end_sec:
            start = result[-1].end_sec
        end = cue.end_sec
        if end - start >= opts.min_cue_seconds and cue.text:
            result.append(SubtitleCue(start_sec=round(start, 3), end_sec=round(end, 3), text=cue.text))
    return result


# --- timeline projection ---------------------------------------------------


def _project(
    start: float,
    end: float,
    timeline_segments: list[TimelineSegment],
    *,
    min_overlap: float,
) -> list[tuple[float, float]]:
    pieces: list[tuple[float, float]] = []
    for timeline in timeline_segments:
        overlap_start = max(start, timeline.source_start_sec)
        overlap_end = min(end, timeline.source_end_sec)
        if overlap_end - overlap_start < min_overlap:
            continue
        out_start = timeline.output_start_sec + (overlap_start - timeline.source_start_sec)
        out_end = timeline.output_start_sec + (overlap_end - timeline.source_start_sec)
        pieces.append((out_start, out_end))
    return pieces


# --- text helpers ----------------------------------------------------------


def _coerce_segments(items: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, dict):
            start = _as_float(item.get("start_sec", item.get("start")))
            end = _as_float(item.get("end_sec", item.get("end")))
            text = str(item.get("text") or "").strip()
            words = _coerce_words(item.get("words") or [])
        else:
            start = _as_float(getattr(item, "start_sec", getattr(item, "start", None)))
            end = _as_float(getattr(item, "end_sec", getattr(item, "end", None)))
            text = str(getattr(item, "text", "") or "").strip()
            words = _coerce_words(getattr(item, "words", None) or [])
        if start is None or end is None or end <= start or not text:
            continue
        result.append({"start_sec": start, "end_sec": end, "text": text, "words": words})
    return sorted(result, key=lambda value: value["start_sec"])


def _coerce_words(items: list[Any]) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, dict):
            start = _as_float(item.get("start_sec", item.get("start")))
            end = _as_float(item.get("end_sec", item.get("end")))
            text = str(item.get("text") or item.get("word") or "").strip()
        else:
            start = _as_float(getattr(item, "start_sec", getattr(item, "start", None)))
            end = _as_float(getattr(item, "end_sec", getattr(item, "end", None)))
            text = str(getattr(item, "text", getattr(item, "word", "")) or "").strip()
        if start is None or end is None or end <= start or not text:
            continue
        words.append({"start_sec": start, "end_sec": end, "text": text})
    return sorted(words, key=lambda word: word["start_sec"])


def _display_card(text: str, opts: CueOptions) -> str:
    """A phrase as it is drawn: same casing and punctuation rules as a word.

    The fallback paths — a transcript with no word timings, or none at all —
    used to skip this, so a style asking for uppercase got it on some clips
    and not others depending on where their words came from.
    """
    words = [_display_word(word, opts) for word in _clean_text(text).split()]
    return _wrap_text(" ".join(word for word in words if word), line_limit=opts.max_line_chars)


def _display_word(text: str, opts: CueOptions) -> str:
    word = _clean_text(text)
    if opts.strip_punctuation:
        word = word.strip(_LEAD_PUNCT + _TRAIL_PUNCT)
    else:
        word = word.strip(_LEAD_PUNCT)
    if opts.uppercase:
        word = word.upper()
    return word


def _wrap_text(text: str, *, line_limit: int = MAX_LINE_CHARS) -> str:
    text = _clean_text(text)
    if len(text) <= line_limit:
        return text
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        added = len(word) + (1 if current else 0)
        if current and current_len + added > line_limit and len(lines) < 1:
            lines.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += added
    if current:
        lines.append(" ".join(current))
    return "\\N".join(lines[:2])


def _clean_text(text: str) -> str:
    text = re.sub(r"\[[^\]]+\]", lambda match: _stage_replacement(match.group(0)), text)
    text = text.replace(">>", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip("-–—:; ")
    return text


def _stage_replacement(value: str) -> str:
    label = value.strip("[]").strip().lower()
    return "" if label in STAGE_DIRECTIONS or not label else value


def _ass_text(text: str) -> str:
    parts = text.replace("\n", "\\N").split("\\N")
    escaped = [
        part.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        for part in parts
    ]
    return "\\N".join(escaped)


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    whole = int(seconds % 60)
    centiseconds = int(round((seconds - int(seconds)) * 100))
    if centiseconds >= 100:
        whole += 1
        centiseconds = 0
    return f"{hours}:{minutes:02d}:{whole:02d}.{centiseconds:02d}"


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
