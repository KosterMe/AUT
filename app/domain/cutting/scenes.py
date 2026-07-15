"""Cutting a video where the picture changes.

For material with no usable speech — films, shows, sport, music edits — the
signal is visual: the editor of the original already decided where one shot
ends and the next begins, and those cuts are the only boundaries that will not
look arbitrary. Ending a clip anywhere else is visible as a clip that stops
mid-shot.

Loudness comes in second, and only to *choose* between candidates rather than
to place them. In a two-hour film there are far more publishable stretches than
anybody wants to post, and the loud ones — action, an argument, a punchline
landing — are the ones worth having when only a handful can be kept.

Pure logic: the scene timestamps and loudness windows are measured elsewhere
(`app.adapters.media.scenes`); this module never touches ffmpeg.
"""
from __future__ import annotations

from typing import Sequence

from app.domain.cutting.base import (
    DEFAULT_GAP_SECONDS,
    MIN_USABLE_SECONDS,
    SliceSpec,
    renumber,
)

# Loudness of a stretch nothing was measured for. Silence in this scale is
# around -70 dB, so an unmeasured clip ranks below a measured quiet one rather
# than winning by default.
UNMEASURED_LOUDNESS_DB = -90.0


def build_scene_slices(
    *,
    source_duration: float,
    scene_changes: Sequence[float],
    loudness: Sequence[tuple[float, float]] = (),
    min_clip_seconds: float,
    max_clip_seconds: float,
    gap_seconds: float = DEFAULT_GAP_SECONDS,
    max_clips: int = 0,
    title_prefix: str = "часть",
) -> list[SliceSpec]:
    """Cut the source into clips that end on a cut in the picture.

    The rule mirrors the speech cutter: grow a clip until it is long enough,
    then end it at the first real boundary. Where there is no scene change
    inside the window at all — a single long take — the clip ends at the
    maximum, because a slightly awkward cut beats a clip that never ends.
    """
    duration = max(0.0, float(source_duration))
    if duration < MIN_USABLE_SECONDS:
        return []

    minimum = max(MIN_USABLE_SECONDS, float(min_clip_seconds))
    maximum = max(minimum, float(max_clip_seconds))
    cuts = _usable_cuts(scene_changes, duration)

    specs: list[SliceSpec] = []
    cursor = 0.0
    index = 1
    while cursor < duration - MIN_USABLE_SECONDS:
        earliest = cursor + minimum
        latest = min(duration, cursor + maximum)
        end = _first_cut_between(cuts, earliest, latest) or latest

        # A tail too short to publish is absorbed rather than left behind.
        if duration - end < MIN_USABLE_SECONDS:
            end = duration

        specs.append(
            SliceSpec(
                index=index,
                start_sec=round(cursor, 3),
                end_sec=round(end, 3),
                title=f"{title_prefix} {index}".strip(),
                text="",
                transcript_segments=0,
            )
        )
        if end >= duration:
            break
        cursor = max(0.0, end - max(0.0, float(gap_seconds)))
        index += 1

    return renumber(_keep_loudest(specs, loudness, max_clips))


def loudness_of(spec: SliceSpec, loudness: Sequence[tuple[float, float]]) -> float:
    """Mean measured level inside a slice, in dB."""
    values = [db for at, db in loudness if spec.start_sec <= at < spec.end_sec]
    return sum(values) / len(values) if values else UNMEASURED_LOUDNESS_DB


def _usable_cuts(scene_changes: Sequence[float], duration: float) -> list[float]:
    return sorted({
        round(float(at), 3)
        for at in scene_changes
        if 0.0 < float(at) < duration
    })


def _first_cut_between(cuts: list[float], earliest: float, latest: float) -> float | None:
    return next((at for at in cuts if earliest <= at <= latest), None)


def _keep_loudest(
    specs: list[SliceSpec], loudness: Sequence[tuple[float, float]], max_clips: int
) -> list[SliceSpec]:
    """Thin the list down to `max_clips`, keeping the liveliest stretches.

    Chronological order is restored afterwards: which parts are kept is a
    judgement about the material, but publishing part 7 before part 3 is just
    wrong.
    """
    if not max_clips or len(specs) <= max_clips:
        return specs
    ranked = sorted(specs, key=lambda spec: (-loudness_of(spec, loudness), spec.start_sec))
    return sorted(ranked[:max_clips], key=lambda spec: spec.start_sec)


__all__ = ["build_scene_slices", "loudness_of"]
