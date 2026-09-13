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

from dataclasses import dataclass, field, fields as dataclass_fields, replace
from typing import Any, Iterable, Mapping

from montage.style import (
    INSERT_FULL,
    INSERT_KINDS,
    INSERT_PIP,
    LAYOUT_AUTO,
    LAYOUT_BLUR,
    LAYOUT_FILL,
    LAYOUT_SPLIT,
    LAYOUTS,
    PLANNABLE_LAYOUTS,
    StyleSpec,
)
from montage.subtitles import SubtitleCue, TimelineSegment

VERSION = 2

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

    What the text *looks like* is deliberately absent: the font, its size and
    where it sits are `style.subtitles`, and having them here as well would be
    two sources of truth for one line of an .ass file.
    """

    cues: tuple[SubtitleCue, ...] = ()
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
    # How all of it looks. Resolved once when the clip is planned and carried
    # here, so a render depends on nothing that could have changed underneath
    # it: the same composition renders the same video a month later.
    style: StyleSpec = field(default_factory=StyleSpec.from_settings)
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
    def crf(self) -> int:
        """Delivery quality. Reads through to the style so there is one of it."""
        return self.style.delivery.crf

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
    style: StyleSpec | None = None,
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

    resolved_style = style or StyleSpec.from_settings()
    return Composition(
        segments=tuple(segments),
        canvas=canvas or canvas_for(resolved_style),
        inserts=tuple(inserts),
        subtitles=subtitles,
        style=resolved_style,
    )


def canvas_for(style: StyleSpec) -> Canvas:
    """The frame a style asks for. The one place the two can disagree."""
    return Canvas(
        width=style.delivery.width, height=style.delivery.height, fps=style.delivery.fps
    )


# ------------------------------------------------------------- serialisation


def to_dict(composition: Composition) -> dict[str, Any]:
    """A composition as plain JSON-able data.

    Stored on the clip it produced, which is what makes a render inspectable
    after the fact and repeatable a month later: everything that shaped the
    video is here, the style included, so nothing has to be guessed from the
    configuration the worker happened to be running at the time.
    """
    return {
        "version": composition.version,
        "canvas": {
            "width": composition.canvas.width,
            "height": composition.canvas.height,
            "fps": composition.canvas.fps,
        },
        "segments": [_asdict(segment) for segment in composition.segments],
        "inserts": [_asdict(insert) for insert in composition.inserts],
        "subtitles": None if composition.subtitles is None else {
            "cues": [_asdict(cue) for cue in composition.subtitles.cues],
            "title_text": composition.subtitles.title_text,
        },
        "music": None if composition.music is None else _asdict(composition.music),
        "effects": [_asdict(effect) for effect in composition.effects],
        "style": composition.style.to_dict(),
    }


def from_dict(data: Mapping[str, Any]) -> Composition:
    """Rebuild a composition stored by `to_dict`.

    Tolerant on the way in: a document written by an older version is missing
    fields that now exist, and the defaults are the right answer for those. A
    document that is structurally wrong still raises — a composition that
    cannot be trusted should not quietly render as something else.
    """
    canvas_data = data.get("canvas") or {}
    canvas = Canvas(
        width=int(canvas_data.get("width", 1080)),
        height=int(canvas_data.get("height", 1920)),
        fps=int(canvas_data.get("fps", 30)),
    )
    subtitles_data = data.get("subtitles")
    subtitles = None
    if isinstance(subtitles_data, Mapping):
        subtitles = SubtitleSpec(
            cues=tuple(
                SubtitleCue(**_only(cue, SubtitleCue))
                for cue in subtitles_data.get("cues") or []
            ),
            title_text=subtitles_data.get("title_text"),
        )
    music_data = data.get("music")
    return Composition(
        segments=tuple(Segment(**_only(item, Segment)) for item in data.get("segments") or []),
        canvas=canvas,
        inserts=tuple(Insert(**_only(item, Insert)) for item in data.get("inserts") or []),
        subtitles=subtitles,
        music=MusicBed(**_only(music_data, MusicBed)) if isinstance(music_data, Mapping) else None,
        effects=tuple(
            SoundEffect(**_only(item, SoundEffect)) for item in data.get("effects") or []
        ),
        style=StyleSpec.from_dict(data.get("style")),
    )


def _asdict(value: Any) -> dict[str, Any]:
    return {f.name: getattr(value, f.name) for f in dataclass_fields(value)}


def _only(data: Mapping[str, Any], kind: type) -> dict[str, Any]:
    """The keys `kind` actually has, so an extra one cannot raise TypeError."""
    known = {f.name for f in dataclass_fields(kind)}
    return {key: value for key, value in data.items() if key in known}


# ------------------------------------------------------------------- excerpts


def excerpt(composition: Composition, *, at_sec: float, duration_sec: float) -> Composition:
    """The window of a composition between `at_sec` and `at_sec + duration_sec`.

    This is what makes a preview cheap. Trimming the description rather than
    the finished file means ffmpeg only ever decodes the seconds being looked
    at — the difference between a second and half a minute — and everything
    laid over the clip moves with it, so what the window shows is what the
    full render would have put there.
    """
    window_start = max(0.0, float(at_sec))
    window_end = min(composition.duration_sec, window_start + max(0.01, float(duration_sec)))
    if window_end <= window_start:
        raise ValueError("an excerpt window has to fall inside the composition")

    segments: list[Segment] = []
    for segment, placed in zip(composition.segments, composition.timeline()):
        start = max(placed.output_start_sec, window_start)
        end = min(placed.output_end_sec, window_end)
        if end - start < MIN_SEGMENT_SECONDS:
            continue
        lead = start - placed.output_start_sec
        segments.append(
            Segment(
                source_path=segment.source_path,
                source_start_sec=round(segment.source_start_sec + lead, 3),
                source_end_sec=round(segment.source_start_sec + lead + (end - start), 3),
                layout=segment.layout,
                companion_path=segment.companion_path,
                # The companion runs continuously under the clip, so it is
                # picked up where the window starts rather than restarted.
                companion_start_sec=round(segment.companion_start_sec + lead, 3),
            )
        )
    if not segments:
        raise ValueError("the excerpt window falls between segments")

    length = round(sum(segment.duration_sec for segment in segments), 3)
    inserts: list[Insert] = []
    for insert in composition.inserts:
        start = max(insert.at_sec, window_start)
        end = min(insert.end_sec, window_end)
        if end - start <= 0.01:
            continue
        at = min(start - window_start, length)
        inserts.append(
            Insert(
                kind=insert.kind,
                source_path=insert.source_path,
                at_sec=round(at, 3),
                duration_sec=round(min(end - start, max(0.01, length - at)), 3),
                source_start_sec=round(insert.source_start_sec + (start - insert.at_sec), 3),
                still=insert.still,
            )
        )

    subtitles = composition.subtitles
    if subtitles is not None:
        subtitles = SubtitleSpec(
            cues=tuple(
                SubtitleCue(
                    start_sec=round(max(cue.start_sec, window_start) - window_start, 3),
                    end_sec=round(min(cue.end_sec, window_end) - window_start, 3),
                    text=cue.text,
                )
                for cue in subtitles.cues
                if cue.end_sec > window_start and cue.start_sec < window_end
            ),
            title_text=subtitles.title_text,
        )

    music = composition.music
    if music is not None:
        # The bed advances with the clip: a preview of the last ten seconds
        # should hear the part of the track that plays there.
        music = replace(music, start_sec=round(music.start_sec + window_start, 3))

    return replace(
        composition,
        segments=tuple(segments),
        inserts=tuple(inserts),
        subtitles=subtitles,
        music=music,
        effects=tuple(
            replace(effect, at_sec=round(effect.at_sec - window_start, 3))
            for effect in composition.effects
            if window_start <= effect.at_sec <= window_end
        ),
    )


def scaled(composition: Composition, factor: float) -> Composition:
    """The same composition rendered into a smaller frame.

    Only the canvas and the type size move: everything positional in this model
    is already expressed as a share of the frame, so a half-size preview is the
    same picture — and the one thing that would not survive being scaled, the
    font size, is in pixels and is scaled here explicitly.
    """
    if not 0.05 <= factor <= 1.0:
        raise ValueError("a preview scale is a fraction of the delivery size")
    canvas = Canvas(
        width=max(2, int(composition.canvas.width * factor) // 2 * 2),
        height=max(2, int(composition.canvas.height * factor) // 2 * 2),
        fps=composition.canvas.fps,
    )
    style = replace(
        composition.style,
        subtitles=composition.style.subtitles.merged(
            {"font_size": max(8, int(composition.style.subtitles.font_size * factor))}
        ),
        delivery=composition.style.delivery.merged(
            {"width": canvas.width, "height": canvas.height}
        ),
    )
    return replace(composition, canvas=canvas, style=style)


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
    "StyleSpec",
    "SubtitleSpec",
    "VERSION",
    "canvas_for",
    "excerpt",
    "from_dict",
    "scaled",
    "single_source",
    "to_dict",
]
