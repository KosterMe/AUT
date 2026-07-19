"""What a clip is made of, described before anything touches ffmpeg.

Until now the montage was a side effect of the render function: it decided
where to cut, what to lay over the picture and how to encode it in one breath,
so the only way to learn what a clip contained was to render it. That holds up
while a clip is one continuous slice of one file. It stops holding up the
moment a second source is involved — an insert, a split screen, a hook taken
from elsewhere in the video.

A `Composition` is that description as data: which pieces of which files play
in what order, what is laid over them, and what is burned on top. It is pure —
no ffmpeg, no database, no filesystem — which is what lets the rules that
*decide* a montage be tested with plain values, and lets the thing that
*renders* it choose freely between rendering strategies.

Two properties are load-bearing for the renderer:

* **Segments are the unit of caching.** A segment describes only what can be
  derived from its source, so its rendered form can be reused across clips and
  across re-renders. Anything style-related — subtitles, inserts, grading —
  is deliberately kept out of it.
* **Times are absolute in the source and implicit in the output.** A segment
  says where it starts and ends in its own file; where it lands in the finished
  clip follows from the segments before it. Nothing has to be renumbered when
  a segment moves.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from app.domain.subtitles import SubtitleCue, TimelineSegment

VERSION = 1

# How a segment fills the canvas.
LAYOUT_BLUR = "blur"    # the source fitted over a blurred, zoomed copy of itself
LAYOUT_FILL = "fill"    # the source cropped to fill the canvas, no backdrop
LAYOUT_SPLIT = "split"  # the source on top, a companion source below
LAYOUTS = (LAYOUT_BLUR, LAYOUT_FILL, LAYOUT_SPLIT)

# What a profile may ask for, as opposed to what a segment may be. "auto" is a
# question — fill or blur, decided from the source's shape when the clip is
# planned — so it is legal to request and never legal to render.
LAYOUT_AUTO = "auto"
PLANNABLE_LAYOUTS = (LAYOUT_AUTO, *LAYOUTS)

# What an insert does to the picture underneath it.
INSERT_FULL = "broll_full"  # covers the frame
INSERT_PIP = "broll_pip"    # a smaller box in a corner
INSERT_KINDS = (INSERT_FULL, INSERT_PIP)

# Shorter than this a segment is not worth a cut — and, more practically, is
# shorter than the seek accuracy of most sources.
MIN_SEGMENT_SECONDS = 0.05


@dataclass(frozen=True)
class Canvas:
    """The frame every segment is rendered into."""

    width: int = 1080
    height: int = 1920
    fps: int = 30

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("canvas dimensions must be positive")
        if self.fps <= 0:
            raise ValueError("canvas fps must be positive")

    @property
    def half_height(self) -> int:
        """Height of one half of a split screen, kept even for yuv420p."""
        return max(2, (self.height // 2) // 2 * 2)


@dataclass(frozen=True)
class Segment:
    """One continuous piece of the output timeline, cut from one source."""

    source_path: str
    source_start_sec: float
    source_end_sec: float
    layout: str = LAYOUT_BLUR
    # Only for LAYOUT_SPLIT: what plays in the bottom half.
    companion_path: str | None = None
    companion_start_sec: float = 0.0

    def __post_init__(self) -> None:
        if not self.source_path:
            raise ValueError("a segment needs a source path")
        if self.layout not in LAYOUTS:
            raise ValueError(f"unknown layout {self.layout!r}; expected one of {LAYOUTS}")
        if self.source_start_sec < 0:
            raise ValueError("a segment cannot start before the source does")
        if self.duration_sec < MIN_SEGMENT_SECONDS:
            raise ValueError(
                f"segment is {self.duration_sec}s long, shorter than the "
                f"{MIN_SEGMENT_SECONDS}s floor"
            )
        if self.layout == LAYOUT_SPLIT and not self.companion_path:
            raise ValueError("a split-screen segment needs a companion source")

    @property
    def duration_sec(self) -> float:
        return round(self.source_end_sec - self.source_start_sec, 3)


@dataclass(frozen=True)
class Insert:
    """Something laid over the assembled video for a stretch of the output.

    Inserts never change timing: the picture underneath keeps running, so
    subtitles and audio stay aligned whether an insert is there or not. That
    is also why they are applied after the segments are joined, and why
    changing them cannot invalidate a cached segment.
    """

    kind: str
    source_path: str
    at_sec: float
    duration_sec: float
    source_start_sec: float = 0.0
    # A still image rather than a video: it has no timeline of its own, so it
    # has to be held on screen for the duration instead of played.
    still: bool = False

    def __post_init__(self) -> None:
        if self.kind not in INSERT_KINDS:
            raise ValueError(f"unknown insert kind {self.kind!r}; expected one of {INSERT_KINDS}")
        if not self.source_path:
            raise ValueError("an insert needs a source path")
        if self.at_sec < 0:
            raise ValueError("an insert cannot start before the clip does")
        if self.duration_sec <= 0:
            raise ValueError("an insert needs a positive duration")

    @property
    def end_sec(self) -> float:
        return round(self.at_sec + self.duration_sec, 3)


@dataclass(frozen=True)
class MusicBed:
    """A track playing under the whole clip, ducked out of the way of speech.

    The level is relative, not absolute: the finished mix is normalised to
    -14 LUFS at the end, so what matters here is how far the music sits below
    whatever else is playing, and the ducking settles the rest.
    """

    source_path: str
    # How far below the rest of the mix the bed sits before ducking.
    gain_db: float = -20.0
    start_sec: float = 0.0
    fade_in_sec: float = 0.6
    fade_out_sec: float = 1.2
    # Sidechain compression: the voice triggers, the music gets out of the way.
    duck_threshold: float = 0.03
    duck_ratio: float = 8.0
    duck_attack_ms: float = 20.0
    duck_release_ms: float = 300.0

    def __post_init__(self) -> None:
        if not self.source_path:
            raise ValueError("a music bed needs a source path")
        if self.start_sec < 0:
            raise ValueError("a music bed cannot start before the track does")
        if not 0.0 < self.duck_threshold <= 1.0:
            raise ValueError("duck_threshold is a linear level in (0, 1]")
        if self.duck_ratio < 1.0:
            raise ValueError("duck_ratio below 1 would boost the music under speech")


@dataclass(frozen=True)
class SoundEffect:
    """A short sound at one moment — a cut, an insert appearing.

    Effects are mixed, never spliced: nothing in the timeline moves to make
    room for one, so adding or removing them cannot desynchronise subtitles.
    """

    source_path: str
    at_sec: float
    duration_sec: float = 1.0
    gain_db: float = -8.0

    def __post_init__(self) -> None:
        if not self.source_path:
            raise ValueError("a sound effect needs a source path")
        if self.at_sec < 0:
            raise ValueError("a sound effect cannot play before the clip starts")
        if self.duration_sec <= 0:
            raise ValueError("a sound effect needs a positive duration")


@dataclass(frozen=True)
class SubtitleSpec:
    """The overlay burned on top: word cues plus the persistent headline.

    Both live here together because they are written into the same .ass file —
    which is why a clip rendered with subtitles disabled still shows its
    headline.
    """

    cues: tuple[SubtitleCue, ...] = ()
    font_size: int = 64
    position_percent: int = 74
    title_text: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.cues and not (self.title_text or "").strip()


@dataclass(frozen=True)
class Composition:
    """A finished clip, described but not yet rendered."""

    segments: tuple[Segment, ...]
    canvas: Canvas = field(default_factory=Canvas)
    inserts: tuple[Insert, ...] = ()
    subtitles: SubtitleSpec | None = None
    music: MusicBed | None = None
    effects: tuple[SoundEffect, ...] = ()
    crf: int = 23
    version: int = VERSION

    def __post_init__(self) -> None:
        if not self.segments:
            raise ValueError("a composition needs at least one segment")
        duration = self.duration_sec
        for insert in self.inserts:
            if insert.end_sec > duration + 0.001:
                raise ValueError(
                    f"insert at {insert.at_sec}s runs {insert.end_sec}s past the end of a "
                    f"{duration}s composition"
                )
        for effect in self.effects:
            if effect.at_sec > duration:
                raise ValueError(
                    f"sound effect at {effect.at_sec}s starts past the end of a "
                    f"{duration}s composition"
                )

    @property
    def duration_sec(self) -> float:
        return round(sum(segment.duration_sec for segment in self.segments), 3)

    @property
    def layouts(self) -> frozenset[str]:
        return frozenset(segment.layout for segment in self.segments)

    @property
    def has_own_audio(self) -> bool:
        """Whether this composition adds audio of its own to the sources'."""
        return self.music is not None or bool(self.effects)

    @property
    def source_paths(self) -> tuple[str, ...]:
        """Every distinct file this composition reads, in first-use order."""
        seen: list[str] = []
        for segment in self.segments:
            for path in (segment.source_path, segment.companion_path):
                if path and path not in seen:
                    seen.append(path)
        for insert in self.inserts:
            if insert.source_path not in seen:
                seen.append(insert.source_path)
        if self.music is not None and self.music.source_path not in seen:
            seen.append(self.music.source_path)
        for effect in self.effects:
            if effect.source_path not in seen:
                seen.append(effect.source_path)
        return tuple(seen)

    def timeline(self) -> list[TimelineSegment]:
        """Where each segment's source time lands in the finished clip.

        This is what subtitle cues are projected onto: a word spoken at
        source second 412 has to be drawn at whatever output second the
        segment carrying it ended up at.
        """
        timeline: list[TimelineSegment] = []
        cursor = 0.0
        for segment in self.segments:
            end = cursor + segment.duration_sec
            timeline.append(
                TimelineSegment(
                    source_start_sec=round(segment.source_start_sec, 3),
                    source_end_sec=round(segment.source_end_sec, 3),
                    output_start_sec=round(cursor, 3),
                    output_end_sec=round(end, 3),
                )
            )
            cursor = end
        return timeline


def single_source(
    source_path: str,
    *,
    start_sec: float,
    end_sec: float,
    keep_segments: Iterable[tuple[float, float]] | None = None,
    canvas: Canvas | None = None,
    layout: str = LAYOUT_BLUR,
    companion_path: str | None = None,
    companion_start_sec: float = 0.0,
    inserts: Iterable[Insert] = (),
    subtitles: SubtitleSpec | None = None,
    crf: int = 23,
) -> Composition:
    """A composition cut from one file — the shape every clip has today.

    `keep_segments` are windows relative to `start_sec`, the form silence
    detection reports. Without them the clip is one continuous segment.
    Windows shorter than the floor are dropped rather than rejected: silence
    detection produces slivers, and losing one is better than losing the clip.

    For a split screen the companion advances across segments rather than
    restarting at each one: the footage in the bottom half is background, and
    background that jumps back on every cut above it draws exactly the
    attention it is there not to draw.
    """
    windows = list(keep_segments) if keep_segments else [(0.0, max(0.0, end_sec - start_sec))]

    segments: list[Segment] = []
    companion_cursor = max(0.0, float(companion_start_sec))
    for rel_start, rel_end in windows:
        rel_start = max(0.0, float(rel_start))
        rel_end = max(rel_start, float(rel_end))
        if rel_end - rel_start < MIN_SEGMENT_SECONDS:
            continue
        segments.append(
            Segment(
                source_path=source_path,
                source_start_sec=round(start_sec + rel_start, 3),
                source_end_sec=round(start_sec + rel_end, 3),
                layout=layout,
                companion_path=companion_path,
                companion_start_sec=round(companion_cursor, 3),
            )
        )
        companion_cursor += rel_end - rel_start

    if not segments:
        segments = [
            Segment(
                source_path=source_path,
                source_start_sec=round(start_sec, 3),
                source_end_sec=round(max(start_sec + MIN_SEGMENT_SECONDS, end_sec), 3),
                layout=layout,
                companion_path=companion_path,
                companion_start_sec=round(max(0.0, float(companion_start_sec)), 3),
            )
        ]

    return Composition(
        segments=tuple(segments),
        canvas=canvas or Canvas(),
        inserts=tuple(inserts),
        subtitles=subtitles,
        crf=crf,
    )


__all__ = [
    "Canvas",
    "Composition",
    "LAYOUT_AUTO",
    "INSERT_FULL",
    "INSERT_KINDS",
    "INSERT_PIP",
    "Insert",
    "LAYOUTS",
    "LAYOUT_BLUR",
    "LAYOUT_FILL",
    "LAYOUT_SPLIT",
    "MIN_SEGMENT_SECONDS",
    "MusicBed",
    "PLANNABLE_LAYOUTS",
    "Segment",
    "SoundEffect",
    "SubtitleSpec",
    "VERSION",
    "single_source",
]
