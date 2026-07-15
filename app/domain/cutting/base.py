"""What every cutter produces, and the rules they all share.

A cutter answers one question — where does each clip start and end — from
whatever signal its material offers: speech for a podcast, scene changes for a
film, the clock for anything else. They differ in the signal and agree on the
output, which is what lets the profile pick one without the rest of the
pipeline knowing which ran.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Clips overlap slightly instead of butting edge-to-edge, so a word spoken
# across a boundary is never lost from both sides.
DEFAULT_GAP_SECONDS = 1.0
# Below this a clip is not worth publishing; used to drop a trailing stub.
MIN_USABLE_SECONDS = 5.0
TITLE_MAX_CHARS = 90


@dataclass(frozen=True)
class SliceSpec:
    """Where one clip starts and ends, and what is said in it."""

    index: int
    start_sec: float
    end_sec: float
    title: str
    text: str
    transcript_segments: int

    @property
    def duration_sec(self) -> float:
        return round(self.end_sec - self.start_sec, 3)


def compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def title_from_text(text: str, index: int) -> str:
    """First sentence of the clip, when it is a sane length for a headline."""
    text = compact_text(text)
    for separator in (".", "!", "?"):
        position = text.find(separator)
        if 12 <= position <= TITLE_MAX_CHARS:
            return text[: position + 1]
    return text[:TITLE_MAX_CHARS].strip() or f"slice {index}"


def renumber(specs: list[SliceSpec]) -> list[SliceSpec]:
    """Give slices consecutive 1-based indexes after filtering or reordering."""
    import dataclasses

    return [dataclasses.replace(spec, index=position) for position, spec in enumerate(specs, 1)]
