"""Cutting by the clock.

No signal at all: fixed-length pieces from start to finish. It exists for
material where nothing better is available — a source with no speech and no
cuts to find, or a job that deliberately wants even parts — and as the floor
every other cutter can fall back to.
"""
from __future__ import annotations

from app.domain.cutting.base import (
    DEFAULT_GAP_SECONDS,
    MIN_USABLE_SECONDS,
    SliceSpec,
    next_start,
)


def build_plain_slices(
    *,
    source_duration: float,
    clip_seconds: float,
    gap_seconds: float = DEFAULT_GAP_SECONDS,
    max_clips: int = 0,
    title_prefix: str = "часть",
) -> list[SliceSpec]:
    """Even pieces covering the whole source.

    The trailing stub is absorbed into the last clip rather than published as a
    four-second fragment: an overlong final part is a worse clip, a tiny final
    part is not a clip at all.
    """
    duration = max(0.0, float(source_duration))
    length = max(MIN_USABLE_SECONDS, float(clip_seconds))
    if duration < MIN_USABLE_SECONDS:
        return []

    specs: list[SliceSpec] = []
    start = 0.0
    index = 1
    while start < duration:
        end = min(duration, start + length)
        if duration - end < MIN_USABLE_SECONDS:
            end = duration
        specs.append(
            SliceSpec(
                index=index,
                start_sec=round(start, 3),
                end_sec=round(end, 3),
                title=f"{title_prefix} {index}".strip(),
                text="",
                transcript_segments=0,
            )
        )
        if end >= duration:
            break
        start = next_start(start, end, gap_seconds)
        index += 1
        if max_clips and len(specs) >= max_clips:
            break

    return specs[:max_clips] if max_clips else specs
