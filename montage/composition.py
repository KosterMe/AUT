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

# 3 is the layered shape: a spine, layers with frames, and audio tracks. 1 and 2
# are the "segments + inserts + music + effects" EDLs, and `from_dict` still
# reads them — a clip rendered last month has its composition stored, and that
# record is the only account of what it was made of.
VERSION = 3

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


@dataclass(frozen=True)
class Segment:
    """One continuous piece of the output timeline, cut from one source.

    How it fills the canvas is a rectangle and a flag, not a word from a list
    of three. `LAYOUT_FILL`, `LAYOUT_BLUR` and `LAYOUT_SPLIT` were three
    branches in the renderer and three values threaded through the profile, the
    API schema, the planner and the cache key, and between them they could
    express exactly three pictures. A frame and a backdrop express those three
    and the ones nobody could ask for before — a source letterboxed on black,
    a source in the upper third, a source inset with a blur behind it.

    The frame is per segment rather than per clip because the fragment cache
    keys on it: a cached piece of spine has already been fitted, and two clips
    that frame the same seconds differently are not the same fragment (§7.4).
    """

    source_path: str
    source_start_sec: float
    source_end_sec: float
    # Where in the canvas this segment's picture goes, and what fills the rest.
    # The default is the blurred backdrop, which is what `layout` defaulted to
    # before it was two fields: it is the framing that never crops anything
    # away, so it is the safe answer for a caller that did not say.
    frame: "Frame" = field(default_factory=lambda: CONTAINED)
    # Whether a blurred copy of this same segment fills what the frame does
    # not. Derived from the segment itself, which is why it is a property of
    # the segment and not a layer: there is no other source to take it from.
    backdrop: bool = True

    def __post_init__(self) -> None:
        if not self.source_path:
            raise ValueError("a segment needs a source path")
        if self.source_start_sec < 0:
            raise ValueError("a segment cannot start before the source does")
        if self.duration_sec < MIN_SEGMENT_SECONDS:
            raise ValueError(
                f"segment is {self.duration_sec}s long, shorter than the "
                f"{MIN_SEGMENT_SECONDS}s floor"
            )

    @property
    def duration_sec(self) -> float:
        return round(self.source_end_sec - self.source_start_sec, 3)


@dataclass(frozen=True)
class Motion:
    """How a frame's numbers change while it is on screen.

    A polyline per property, in **output seconds**, with the easing already
    applied by `scenario.curve`: a spring and a hand-drawn wiggle reach the
    renderer as the same kind of thing — points, with the value moving
    linearly between them. That is what keeps this a description of what
    happens rather than a plan somebody still has to interpret, and it is why
    the renderer needs one expression builder instead of one per easing.

    Position only, for now, and the reason is measured rather than assumed
    (§7.2): `overlay` takes expressions in `t` and animates on every frame;
    scale needs `zoompan`, which counts frames rather than seconds; an
    arbitrary opacity curve is `geq`, which costs ×23; rotation costs ×3.3 and
    changes the box. Those wait for a pass of their own rather than arriving
    half-working.
    """

    # (second of the output, value in per cent of the canvas)
    x: tuple[tuple[float, float], ...] = ()
    y: tuple[tuple[float, float], ...] = ()

    @property
    def moves(self) -> bool:
        return bool(self.x or self.y)


@dataclass(frozen=True)
class Frame:
    """Where something sits in the canvas, in per cent rather than pixels.

    Per cent because a composition should survive a change of canvas, and
    because the alternative is what this replaced: a corner computed as
    `canvas.width - box_width - 48` inside the renderer, which is a layout
    decision that nobody outside the renderer could see or change.

    `x` and `y` are the centre. A layer whose height follows from its width —
    a picture-in-picture keeping its aspect ratio — leaves `height` at 0, and
    the renderer positions it with ffmpeg's own `w`/`h` variables rather than
    guessing what the scale will produce.
    """

    x: float = 50.0
    y: float = 50.0
    width: float = 100.0
    # 0 means "whatever the aspect ratio gives", which is not the same as 100.
    height: float = 0.0
    fit: str = "cover"
    opacity: float = 1.0
    rotate: float = 0.0
    # None means it does not move, which is the common case and has to stay
    # the cheap one: a composition with no animation must compile to the same
    # filter string it compiled to before animation existed (§4.3).
    motion: "Motion | None" = None

    @property
    def moves(self) -> bool:
        return self.motion is not None and self.motion.moves

    @property
    def fills_canvas(self) -> bool:
        """Whether this covers the frame edge to edge, centred and upright.

        The common case by a wide margin, and worth knowing about: it compiles
        to a scale and a crop rather than to positioning arithmetic.
        """
        return (
            (self.x, self.y) == (50.0, 50.0)
            and self.width >= 100.0
            and self.height >= 100.0
            and self.opacity == 1.0
            and self.rotate == 0.0
            # A moving frame is not a frame that fills the canvas, even while
            # it is passing through the middle of it: the shortcut this
            # unlocks is a scale and a crop with no positioning at all.
            and not self.moves
        )

    def box(self, canvas: "Canvas") -> tuple[int, int, int, int]:
        """This frame in whole pixels of `canvas`: left, top, width, height.

        Widths are rounded to even numbers because yuv420p subsamples chroma by
        two and an odd dimension is rejected outright. Height comes back as 0
        when the frame leaves it to the aspect ratio.
        """
        width = max(2, int(canvas.width * self.width / 100.0) // 2 * 2)
        height = (
            max(2, int(canvas.height * self.height / 100.0) // 2 * 2)
            if self.height else 0
        )
        left = int(round(canvas.width * self.x / 100.0 - width / 2))
        top = (
            int(round(canvas.height * self.y / 100.0 - height / 2))
            if height else int(round(canvas.height * self.y / 100.0))
        )
        return left, top, width, height




FULL_FRAME = Frame(width=100.0, height=100.0)
# Fitted inside the canvas rather than cropped to it, which is what leaves room
# for a backdrop to show.
CONTAINED = Frame(width=100.0, height=100.0, fit="contain")
TOP_HALF = Frame(y=25.0, width=100.0, height=50.0)
BOTTOM_HALF = Frame(y=75.0, width=100.0, height=50.0)


@dataclass(frozen=True)
class Layer:
    """Something laid over the spine for a stretch of the output.

    The generalisation of what used to be an `Insert` with a `kind`. There is
    no kind any more: `broll_full` and `broll_pip` were a frame covering the
    canvas and a frame in the corner, which is two values of `frame` and not
    two sorts of thing. Adding a third position used to mean adding a third
    branch to the renderer; now it means writing down a rectangle.

    Layers never change timing: the picture underneath keeps running, so
    subtitles and audio stay aligned whether a layer is there or not. That is
    also why they are applied after the spine is joined, and why changing one
    cannot invalidate a cached fragment.
    """

    source_path: str
    at_sec: float
    duration_sec: float
    source_start_sec: float = 0.0
    # A still image rather than a video: it has no timeline of its own, so it
    # has to be held on screen for the duration instead of played.
    still: bool = False
    frame: Frame = field(default_factory=lambda: FULL_FRAME)
    # Bigger sits on top. Ties keep the order they were emitted in.
    z: int = 0

    def __post_init__(self) -> None:
        if not self.source_path:
            raise ValueError("a layer needs a source path")
        if self.at_sec < 0:
            raise ValueError("a layer cannot start before the clip does")
        if self.duration_sec <= 0:
            raise ValueError("a layer needs a positive duration")

    @property
    def end_sec(self) -> float:
        return round(self.at_sec + self.duration_sec, 3)


@dataclass(frozen=True)
class AudioTrack:
    """One sound laid under or over the clip.

    `MusicBed` and `SoundEffect` were the same structure with different
    defaults: a bed is a track that loops and ducks under speech, an effect is
    a short one that does not. Having two of them is why adding a third —
    a voiceover, say — meant a third code path rather than a third set of
    values.

    Ducking is a sidechain compressor keyed on the speech. A threshold above
    any real signal is a compressor that never fires, which is how a track that
    should stay at its own level says so.
    """

    source_path: str
    at_sec: float = 0.0
    # 0 runs to the end of the clip, which is what a bed does.
    duration_sec: float = 0.0
    gain_db: float = -20.0
    # Where in the track to start, so a bed does not open with the same four
    # bars on every clip of a job.
    source_start_sec: float = 0.0
    loop: bool = False
    fade_in_sec: float = 0.0
    fade_out_sec: float = 0.0
    duck_threshold: float = 1.0
    duck_ratio: float = 1.0
    duck_attack_ms: float = 20.0
    duck_release_ms: float = 300.0

    def __post_init__(self) -> None:
        if not self.source_path:
            raise ValueError("an audio track needs a source path")
        if self.at_sec < 0:
            raise ValueError("an audio track cannot start before the clip does")
        if self.source_start_sec < 0:
            raise ValueError("an audio track cannot start before the file does")
        if not 0.0 < self.duck_threshold <= 1.0:
            raise ValueError("duck_threshold is a linear level in (0, 1]")
        if self.duck_ratio < 1.0:
            raise ValueError("duck_ratio below 1 would boost the track under speech")

    @property
    def ducks(self) -> bool:
        """Whether this track gets out of the way of speech."""
        return self.duck_threshold < 1.0 and self.duck_ratio > 1.0


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
    """A finished clip, described but not yet rendered.

    Three lists and a spine. The spine decides how long the clip is; layers sit
    over it in z-order and cannot move it; audio tracks play under it. What
    used to be `segments + inserts + music + effects` said the same thing in
    four shapes, two of which differed only in their defaults.
    """

    spine: tuple[Segment, ...]
    canvas: Canvas = field(default_factory=Canvas)
    layers: tuple[Layer, ...] = ()
    subtitles: SubtitleSpec | None = None
    audio: tuple[AudioTrack, ...] = ()
    # How all of it looks. Resolved once when the clip is planned and carried
    # here, so a render depends on nothing that could have changed underneath
    # it: the same composition renders the same video a month later.
    style: StyleSpec = field(default_factory=StyleSpec.from_settings)
    version: int = VERSION

    def __post_init__(self) -> None:
        if not self.spine:
            raise ValueError("a composition needs at least one segment")
        duration = self.duration_sec
        for layer in self.layers:
            if layer.end_sec > duration + 0.001:
                raise ValueError(
                    f"layer at {layer.at_sec}s runs {layer.end_sec}s past the end of a "
                    f"{duration}s composition"
                )
        for track in self.audio:
            if track.at_sec > duration:
                raise ValueError(
                    f"audio track at {track.at_sec}s starts past the end of a "
                    f"{duration}s composition"
                )

    @property
    def duration_sec(self) -> float:
        return round(sum(segment.duration_sec for segment in self.spine), 3)

    @property
    def crf(self) -> int:
        """Delivery quality. Reads through to the style so there is one of it."""
        return self.style.delivery.crf

    @property
    def framings(self) -> frozenset[tuple]:
        """The distinct ways this spine fills the canvas.

        More than one means the graph has to build more than one kind of
        segment chain, which is what the one-pass ceiling is really counting.
        """
        return frozenset(
            (segment.frame, segment.backdrop) for segment in self.spine
        )

    @property
    def has_own_audio(self) -> bool:
        """Whether this composition adds audio of its own to the sources'."""
        return bool(self.audio)

    @property
    def beds(self) -> tuple[AudioTrack, ...]:
        """Tracks that run under the clip rather than firing at a moment."""
        return tuple(track for track in self.audio if track.loop)

    @property
    def stingers(self) -> tuple[AudioTrack, ...]:
        """Tracks that fire once, at a moment."""
        return tuple(track for track in self.audio if not track.loop)

    @property
    def stack(self) -> tuple[Layer, ...]:
        """Layers bottom to top: by z, then by when they appear.

        One ordering in one place. The renderer applies them in this order and
        `overlay` has no z of its own, so the list *is* the stack.
        """
        return tuple(sorted(self.layers, key=lambda layer: (layer.z, layer.at_sec)))

    @property
    def source_paths(self) -> tuple[str, ...]:
        """Every distinct file this composition reads, in first-use order."""
        seen: list[str] = []
        for segment in self.spine:
            if segment.source_path not in seen:
                seen.append(segment.source_path)
        for layer in self.stack:
            if layer.source_path not in seen:
                seen.append(layer.source_path)
        for track in self.audio:
            if track.source_path not in seen:
                seen.append(track.source_path)
        return tuple(seen)

    def timeline(self) -> list[TimelineSegment]:
        """Where each segment's source time lands in the finished clip.

        This is what subtitle cues are projected onto: a word spoken at
        source second 412 has to be drawn at whatever output second the
        segment carrying it ended up at.
        """
        timeline: list[TimelineSegment] = []
        cursor = 0.0
        for segment in self.spine:
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
    frame: "Frame | None" = None,
    backdrop: bool = True,
    layers: Iterable[Layer] = (),
    subtitles: SubtitleSpec | None = None,
    style: StyleSpec | None = None,
) -> Composition:
    """A composition cut from one file — the shape every clip has today.

    `keep_segments` are windows relative to `start_sec`, the form silence
    detection reports. Without them the clip is one continuous segment.
    Windows shorter than the floor are dropped rather than rejected: silence
    detection produces slivers, and losing one is better than losing the clip.

    The frame and the backdrop apply to every segment alike: one call describes
    one clip cut from one file, and a clip that framed its own pieces
    differently would not be one clip.
    """
    windows = list(keep_segments) if keep_segments else [(0.0, max(0.0, end_sec - start_sec))]

    shape = frame or CONTAINED
    segments: list[Segment] = []
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
                frame=shape,
                backdrop=backdrop,
            )
        )

    if not segments:
        segments = [
            Segment(
                source_path=source_path,
                source_start_sec=round(start_sec, 3),
                source_end_sec=round(max(start_sec + MIN_SEGMENT_SECONDS, end_sec), 3),
                frame=shape,
                backdrop=backdrop,
            )
        ]

    resolved_style = style or StyleSpec.from_settings()
    return Composition(
        spine=tuple(segments),
        canvas=canvas or canvas_for(resolved_style),
        layers=tuple(layers),
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
        "spine": [
            {**_asdict(segment), "frame": _frame_dict(segment.frame)}
            for segment in composition.spine
        ],
        "layers": [
            {**_asdict(layer), "frame": _frame_dict(layer.frame)}
            for layer in composition.stack
        ],
        "subtitles": None if composition.subtitles is None else {
            "cues": [_asdict(cue) for cue in composition.subtitles.cues],
            "title_text": composition.subtitles.title_text,
        },
        "audio": [_asdict(track) for track in composition.audio],
        "style": composition.style.to_dict(),
    }


def from_dict(data: Mapping[str, Any]) -> Composition:
    """Rebuild a composition stored by `to_dict`, whichever shape it is in.

    Tolerant on the way in: a document written by an older version is missing
    fields that now exist, and the defaults are the right answer for those. A
    document that is structurally wrong still raises — a composition that
    cannot be trusted should not quietly render as something else.

    Versions 1 and 2 are lifted rather than rejected. Every clip rendered
    before this carries one, and that stored composition is the only record of
    what the clip was made of; a reader that could not open it would turn every
    one of them into a video nobody can account for.
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

    style = StyleSpec.from_dict(data.get("style"))
    spine, companion = _spine_from(data, canvas)
    return Composition(
        spine=spine,
        canvas=canvas,
        layers=_layers_from(data, style, canvas) + companion,
        subtitles=subtitles,
        audio=_audio_from(data),
        style=style,
    )


def _spine_from(
    data: Mapping[str, Any], canvas: Canvas
) -> tuple[tuple[Segment, ...], tuple[Layer, ...]]:
    """The spine, and whatever an older document's layout implied beside it.

    A layout named one of three pictures, and each is a frame: `fill` covers
    the canvas, `blur` is contained inside it with a blurred copy behind, and
    `split` puts the source in the top half and something else in the bottom.
    The third is the one that produces a layer as well — the companion was a
    second source all along, sitting in the half the spine does not fill.
    """
    if data.get("spine") is not None:
        rows = data["spine"]
        return tuple(
            Segment(
                **{k: v for k, v in _only(row, Segment).items() if k != "frame"},
                frame=_frame_from(row.get("frame")),
            )
            for row in rows
        ), ()

    segments: list[Segment] = []
    companion_path = ""
    companion_start = 0.0
    for row in data.get("segments") or []:
        layout = row.get("layout", LAYOUT_BLUR)
        fields = _only(row, Segment)
        fields.pop("frame", None)
        fields.pop("backdrop", None)
        segments.append(Segment(
            **fields,
            frame=frame_for_layout(layout),
            backdrop=layout == LAYOUT_BLUR,
        ))
        if layout == LAYOUT_SPLIT and row.get("companion_path") and not companion_path:
            companion_path = row["companion_path"]
            companion_start = float(row.get("companion_start_sec") or 0.0)

    if not companion_path:
        return tuple(segments), ()

    # The companion ran continuously under every segment, picked up rather than
    # restarted on each cut — which is one layer spanning the clip, and always
    # was. Its per-segment cursor existed only because a segment was the only
    # place to keep it.
    length = round(sum(segment.duration_sec for segment in segments), 3)
    return tuple(segments), (Layer(
        source_path=companion_path,
        at_sec=0.0,
        duration_sec=length,
        source_start_sec=companion_start,
        frame=BOTTOM_HALF,
        z=-1,
    ),)


def frame_for_layout(layout: str) -> Frame:
    """The rectangle a layout named."""
    if layout == LAYOUT_SPLIT:
        return TOP_HALF
    if layout == LAYOUT_FILL:
        return FULL_FRAME
    # `blur` and `auto`: the picture is contained in the canvas, and whatever
    # it does not cover is the blurred copy behind it.
    return CONTAINED


def _layers_from(data: Mapping[str, Any], style: StyleSpec, canvas: Canvas) -> tuple[Layer, ...]:
    """Layers, or the inserts of an older document read as layers.

    The kind an old insert carries is the frame it meant: `broll_full` covered
    the canvas, `broll_pip` sat in the corner at the size the insert policy
    gave it. Turning the word back into the rectangle is the whole of the
    migration, because the word never meant anything else.
    """
    if data.get("layers") is not None:
        out = []
        for item in data["layers"]:
            frame_data = item.get("frame") if isinstance(item, Mapping) else None
            frame = _frame_from(frame_data)
            fields = {k: v for k, v in _only(item, Layer).items() if k != "frame"}
            out.append(Layer(**fields, frame=frame))
        return tuple(out)

    return tuple(
        Layer(
            **{k: v for k, v in _only(item, Layer).items() if k != "frame"},
            frame=frame_for_kind(item.get("kind", INSERT_FULL), style.inserts, canvas),
        )
        for item in data.get("inserts") or []
    )


def frame_for_kind(kind: str, policy, canvas: Canvas) -> Frame:
    """The frame an old insert kind stood for.

    Two presets, which is what the two kinds always were. The corner is the one
    the renderer used to compute inline — `canvas.width - box_width - margin` —
    written here as per cent of a canvas so it can be moved without editing a
    filter string. The canvas is needed because the old margin was in pixels
    and a frame is not; converting it is the one place the two vocabularies
    have to meet.
    """
    if kind != INSERT_PIP:
        return FULL_FRAME
    box_width = max(2, int(canvas.width * policy.pip_width_share) // 2 * 2)
    left = canvas.width - box_width - policy.pip_margin_px
    return Frame(
        x=(left + box_width / 2.0) / canvas.width * 100.0,
        y=policy.pip_top_share * 100.0,
        width=box_width / canvas.width * 100.0,
        height=0.0,          # the aspect ratio decides, as `scale=W:-2` did
        fit="contain",
    )


def _audio_from(data: Mapping[str, Any]) -> tuple[AudioTrack, ...]:
    """Audio tracks, or an older document's bed and stingers read as tracks."""
    if data.get("audio") is not None:
        return tuple(AudioTrack(**_only(item, AudioTrack)) for item in data["audio"])

    tracks: list[AudioTrack] = []
    bed = data.get("music")
    if isinstance(bed, Mapping):
        # A bed's `start_sec` was an offset into the *track*, not into the clip
        # — it exists so a job's clips do not all open on the same four bars.
        # The new name says which, because the old one could be read either way.
        fields = _only(bed, AudioTrack)
        fields.pop("at_sec", None)
        fields["source_start_sec"] = float(bed.get("start_sec") or 0.0)
        tracks.append(AudioTrack(**fields, loop=True))
    for item in data.get("effects") or []:
        tracks.append(AudioTrack(**_only(item, AudioTrack)))
    return tuple(tracks)


def _asdict(value: Any) -> dict[str, Any]:
    return {f.name: getattr(value, f.name) for f in dataclass_fields(value)}


def _shifted(frame: Frame, by: float) -> Frame:
    """The same rectangle with its curves moved onto another clock."""
    if frame.motion is None or not by:
        return frame
    def move(points: tuple[tuple[float, float], ...]) -> tuple[tuple[float, float], ...]:
        return tuple((round(at - by, 3), value) for at, value in points)
    return replace(frame, motion=Motion(x=move(frame.motion.x), y=move(frame.motion.y)))


def _frame_dict(frame: Frame) -> dict[str, Any]:
    """A frame as data, motion included.

    Written out by hand rather than by `asdict` because the curve is tuples of
    tuples, and JSON has one kind of sequence.
    """
    data = _asdict(frame)
    motion = data.pop("motion", None)
    data["motion"] = None if motion is None else {
        "x": [[at, value] for at, value in motion.x],
        "y": [[at, value] for at, value in motion.y],
    }
    return data


def _frame_from(data: Any, default: Frame = FULL_FRAME) -> Frame:
    """A frame read back. A document written before motion existed has none."""
    if not isinstance(data, Mapping):
        return default
    fields = _only(data, Frame)
    motion = fields.pop("motion", None)
    return Frame(**fields, motion=_motion_from(motion))


def _motion_from(data: Any) -> "Motion | None":
    if not isinstance(data, Mapping):
        return None
    motion = Motion(x=_curve_from(data.get("x")), y=_curve_from(data.get("y")))
    return motion if motion.moves else None


def _curve_from(data: Any) -> tuple[tuple[float, float], ...]:
    if not isinstance(data, (list, tuple)):
        return ()
    return tuple(
        (float(point[0]), float(point[1]))
        for point in data
        if isinstance(point, (list, tuple)) and len(point) == 2
    )


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
    for segment, placed in zip(composition.spine, composition.timeline()):
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
                frame=segment.frame,
                backdrop=segment.backdrop,
            )
        )
    if not segments:
        raise ValueError("the excerpt window falls between segments")

    length = round(sum(segment.duration_sec for segment in segments), 3)
    layers: list[Layer] = []
    for layer in composition.stack:
        start = max(layer.at_sec, window_start)
        end = min(layer.end_sec, window_end)
        if end - start <= 0.01:
            continue
        at = min(start - window_start, length)
        layers.append(replace(
            layer,
            at_sec=round(at, 3),
            duration_sec=round(min(end - start, max(0.01, length - at)), 3),
            source_start_sec=round(layer.source_start_sec + (start - layer.at_sec), 3),
            # A curve is written against the composition's clock, and an
            # excerpt starts its own at zero. Without this a preview of a
            # moving layer shows it parked at the value it starts from, which
            # is the one thing a preview of an animation must not do.
            frame=_shifted(layer.frame, window_start),
        ))

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

    # A bed advances with the clip — a preview of the last ten seconds should
    # hear the part of the track that plays there — while a stinger fires at a
    # moment and moves with the window or falls outside it.
    audio = tuple(
        replace(track, source_start_sec=round(track.source_start_sec + window_start, 3))
        for track in composition.beds
    ) + tuple(
        replace(track, at_sec=round(track.at_sec - window_start, 3))
        for track in composition.stingers
        if window_start <= track.at_sec <= window_end
    )

    return replace(
        composition,
        spine=tuple(segments),
        layers=tuple(layers),
        subtitles=subtitles,
        audio=audio,
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
    "Frame",
    "LAYOUTS",
    "LAYOUT_BLUR",
    "LAYOUT_FILL",
    "LAYOUT_SPLIT",
    "MIN_SEGMENT_SECONDS",
    "Layer",
    "PLANNABLE_LAYOUTS",
    "Segment",
    "AudioTrack",
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
