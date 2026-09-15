"""Cutters: where a long video becomes a list of clips.

Three signals, one output. Which one runs is a property of the profile, not of
the caller — `app.domain.profiles` names a cutter and everything downstream
sees the same `SliceSpec` list either way.

* `speech` — sentence ends. The default, and the only one that understands
  what is being said.
* `scenes` — cuts in the picture, ranked by loudness. For material with no
  usable dialogue.
* `plain` — the clock. The floor when neither signal exists.
"""
from __future__ import annotations

from app.domain.cutting.base import (
    DEFAULT_GAP_SECONDS,
    MIN_USABLE_SECONDS,
    TITLE_MAX_CHARS,
    SliceSpec,
    compact_text,
    renumber,
    title_from_text,
)
from app.domain.cutting.plain import build_plain_slices
from app.domain.cutting.scenes import build_scene_slices, loudness_of
from app.domain.cutting.speech import (
    STRONG_PAUSE_SECONDS,
    WEAK_PAUSE_SECONDS,
    build_semantic_slices,
    is_sentence_boundary,
)

CUTTER_SPEECH = "speech"
CUTTER_SCENES = "scenes"
CUTTER_PLAIN = "plain"
CUTTERS = (CUTTER_SPEECH, CUTTER_SCENES, CUTTER_PLAIN)

# Which cutters cannot work without words. Declared here, beside the cutters
# themselves, because it is the cutter's own need and nobody else's: the speech
# cutter reads sentence ends, the other two read the picture and the clock.
#
# It used to be `profiles.requires_transcript`, which made the montage side
# decide whether ASR ran during cutting — a profile asking for subtitles would
# transcribe a source its own cutter never looked at. Subtitles are welcome to
# want a transcript; they are not the reason the cutting stage runs one.
NEEDS_TRANSCRIPT = (CUTTER_SPEECH,)


def needs_transcript(cutter: str) -> bool:
    """Whether this cutter cannot do its job without a transcript."""
    return cutter in NEEDS_TRANSCRIPT

__all__ = [
    "CUTTERS",
    "NEEDS_TRANSCRIPT",
    "CUTTER_PLAIN",
    "CUTTER_SCENES",
    "CUTTER_SPEECH",
    "DEFAULT_GAP_SECONDS",
    "MIN_USABLE_SECONDS",
    "STRONG_PAUSE_SECONDS",
    "SliceSpec",
    "TITLE_MAX_CHARS",
    "WEAK_PAUSE_SECONDS",
    "build_plain_slices",
    "build_scene_slices",
    "build_semantic_slices",
    "compact_text",
    "is_sentence_boundary",
    "loudness_of",
    "needs_transcript",
    "renumber",
    "title_from_text",
]
