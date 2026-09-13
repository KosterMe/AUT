"""The scenario: a montage described before the video exists.

An ordinary editor edits material — this file is 4:12 long, 1920x1080, here is
its waveform. A scenario is written once and applied to hundreds of clips, each
a different length and shape, so nothing in it can name a second or a pixel.
Three substitutions carry the whole design:

* a **slot** instead of a file — a promise that something will be here, and a
  description of where to get it;
* an **anchor** instead of a second — "three in", "two from the end", "half
  way", "on the word 'car'";
* **per cent of the canvas** instead of pixels, with a fit mode to absorb a
  source whose shape is not known yet.

Everything is a frozen dataclass and nothing here reads a file, queries a
database or runs ffmpeg. That is what lets the compiler be tested by value, and
what makes a scenario a thing you can store, diff and hand to an editor.

`app.domain.style.StyleSpec` is reused rather than restated. The design sketch
(§4) lists `look`, `subtitles` and `audio` as three fields of their own; they
are exactly three of that spec's groups, the renderer already consumes it, and
`Composition` already carries one — so a second vocabulary for the same
settings would have to be kept in step with the first forever. See trap 19.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from montage.composition import Canvas
from montage.style import StyleSpec

VERSION = 1

# --- what an element is made of ---------------------------------------------

SLOT_SOURCE = "source"           # the clip itself, the reason for all of this
SLOT_SOURCE_AT = "source_at"     # elsewhere in the same source, found by event
SLOT_LIBRARY = "library"         # from the asset library, by tag
SLOT_UPLOAD = "upload"           # one particular uploaded file
SLOT_COLOR = "color"             # a flat fill
SLOT_GRADIENT = "gradient"
SLOT_TEXT = "text"               # a template with substitutions
SLOT_BLUR_OF = "blur_of"         # a blurred copy of another element
SLOT_KINDS = (
    SLOT_SOURCE, SLOT_SOURCE_AT, SLOT_LIBRARY, SLOT_UPLOAD,
    SLOT_COLOR, SLOT_GRADIENT, SLOT_TEXT, SLOT_BLUR_OF,
)

# How a library slot picks between several assets carrying its tag.
PICK_ROTATE = "rotate"           # least recently used, the current behaviour
PICK_RANDOM = "random"           # seeded by the clip, never by the clock
PICK_FIRST = "first"
PICKS = (PICK_ROTATE, PICK_RANDOM, PICK_FIRST)


@dataclass(frozen=True)
class EventRef:
    """A moment found in the material rather than named in seconds."""

    kind: str = "cut"        # cut | word | loudest | silence | sentence | beat
    index: int = 0           # 0 is the first; -1 the last
    word: str = ""           # for kind="word"


@dataclass(frozen=True)
class Slot:
    """What an element is, before anything is known about the material."""

    kind: str = SLOT_SOURCE
    tag: str = ""                       # library
    pick: str = PICK_ROTATE             # library
    event: EventRef | None = None       # source_at
    upload_id: int | None = None        # upload
    color: str = ""                     # color / gradient
    template: str = ""                  # text, with {title} {index} {tags}
    ref: str = ""                       # blur_of: the id of another element

    def __post_init__(self) -> None:
        if self.kind not in SLOT_KINDS:
            raise ValueError(f"unknown slot {self.kind!r}; expected one of {SLOT_KINDS}")
        if self.pick not in PICKS:
            raise ValueError(f"unknown pick {self.pick!r}; expected one of {PICKS}")
        if self.kind == SLOT_LIBRARY and not self.tag:
            raise ValueError("a library slot needs a tag to look for")
        if self.kind == SLOT_BLUR_OF and not self.ref:
            raise ValueError("a blur_of slot needs the element it is a blur of")
        if self.kind == SLOT_SOURCE_AT and self.event is None:
            raise ValueError("a source_at slot needs an event to find")
        if self.kind == SLOT_UPLOAD and self.upload_id is None:
            raise ValueError("an upload slot needs an upload id")


# --- time (§4.1) -------------------------------------------------------------


class AnchorMode(str, Enum):
    START = "start"              # n seconds in
    END = "end"                  # n seconds before the end
    FRACTION = "fraction"        # a share of the length
    EVENT = "event"              # wherever the material says
    AFTER = "after"              # relative to another element
    BEFORE = "before"


@dataclass(frozen=True)
class Anchor:
    """When something happens, on a clip whose length is not known yet.

    The same problem as `top: 20px` / `bottom: 20px` / `top: 50%`: three
    different intentions that coincide in one static layout and come apart in
    every other. Storing the second instead of the intention is what makes a
    montage that only works on the clip it was built against.
    """

    mode: AnchorMode = AnchorMode.START
    value: float = 0.0
    event: EventRef | None = None
    ref: str | None = None               # id of another element
    offset_sec: float = 0.0

    def __post_init__(self) -> None:
        if self.mode == AnchorMode.EVENT and self.event is None:
            raise ValueError("an event anchor needs an event")
        if self.mode in (AnchorMode.AFTER, AnchorMode.BEFORE) and not self.ref:
            raise ValueError(f"a {self.mode.value} anchor needs the element it is relative to")
        if self.mode == AnchorMode.FRACTION and not 0.0 <= self.value <= 1.0:
            raise ValueError("a fraction anchor takes 0.0 to 1.0")


class DurationMode(str, Enum):
    FIXED = "fixed"              # seconds
    FRACTION = "fraction"        # a share of the clip
    NATURAL = "natural"          # the asset's own length
    ELASTIC = "elastic"          # whatever is left — spine only
    UNTIL = "until"              # up to another anchor


@dataclass(frozen=True)
class Duration:
    """How long something lasts, and how it behaves when there is not room.

    `grow`, `min_sec` and `max_sec` are the flexbox vocabulary on purpose: the
    spine is a row of boxes sharing a width nobody knows in advance, which is
    the same problem with the same answer.
    """

    mode: DurationMode = DurationMode.FIXED
    value: float = 3.0
    grow: float = 1.0
    min_sec: float = 0.0
    max_sec: float = 0.0                 # 0 means no ceiling
    until: Anchor | None = None

    def __post_init__(self) -> None:
        if self.mode == DurationMode.UNTIL and self.until is None:
            raise ValueError("an 'until' duration needs an anchor to run up to")
        if self.mode == DurationMode.FIXED and self.value <= 0:
            raise ValueError("a fixed duration must be positive")
        if self.min_sec < 0 or self.max_sec < 0:
            raise ValueError("duration bounds cannot be negative")
        if self.max_sec and self.max_sec < self.min_sec:
            raise ValueError("max_sec is below min_sec")
        if self.grow < 0:
            raise ValueError("grow cannot be negative")


# --- space and animation (§4.2, §4.3) ----------------------------------------


@dataclass(frozen=True)
class Keyframe:
    at: Anchor
    value: float
    easing: str = "linear"               # linear | in | out | in_out | step


@dataclass(frozen=True)
class Animated:
    """A number that may or may not move.

    `is_static` is not decoration. A scenario with no animation has to compile
    to the same filter string it compiles to today, or every unanimated clip
    starts paying for animation it does not have.
    """

    static: float
    keys: tuple[Keyframe, ...] = ()

    @property
    def is_static(self) -> bool:
        return not self.keys


@dataclass(frozen=True)
class Rect:
    """A window into a source, in per cent of its own size."""

    x: float = 0.0
    y: float = 0.0
    width: float = 100.0
    height: float = 100.0


FIT_COVER = "cover"
FIT_CONTAIN = "contain"
FIT_FILL = "fill"
FIT_NONE = "none"
# Decides between cover and contain from the source's own shape, which is only
# known once there is a file. It is `choose_layout` — the rule the compiler used
# to branch on — turned into a value a scenario can carry (§9.1).
FIT_AUTO = "auto"
FITS = (FIT_AUTO, FIT_COVER, FIT_CONTAIN, FIT_FILL, FIT_NONE)


@dataclass(frozen=True)
class Frame:
    """Where in the canvas an element sits, as per cent rather than pixels.

    Per cent so the scenario survives a change of canvas: the same montage has
    to mean something at 1080x1920 and at 1080x1350.
    """

    x: Animated = field(default_factory=lambda: Animated(50.0))
    y: Animated = field(default_factory=lambda: Animated(50.0))
    width: Animated = field(default_factory=lambda: Animated(100.0))
    height: Animated = field(default_factory=lambda: Animated(100.0))
    rotate: Animated = field(default_factory=lambda: Animated(0.0))
    opacity: Animated = field(default_factory=lambda: Animated(1.0))
    fit: str = FIT_COVER
    align: str = "center"
    radius: float = 0.0
    crop: Rect | None = None
    blend: str = "normal"

    def __post_init__(self) -> None:
        if self.fit not in FITS:
            raise ValueError(f"unknown fit {self.fit!r}; expected one of {FITS}")

    @property
    def is_static(self) -> bool:
        return all(
            value.is_static for value in
            (self.x, self.y, self.width, self.height, self.rotate, self.opacity)
        )

    @property
    def fills_canvas(self) -> bool:
        """Whether this frame is the whole canvas and nothing but.

        The spine of the default scenario is exactly this, and a frame that
        fills the canvas needs no overlay at all — which is how a scenario
        without layers compiles to the graph the renderer builds today.
        """
        return (
            self.is_static
            and (self.x.static, self.y.static) == (50.0, 50.0)
            and (self.width.static, self.height.static) == (100.0, 100.0)
            and self.rotate.static == 0.0
            and self.opacity.static == 1.0
            and self.crop is None
        )


@dataclass(frozen=True)
class Effect:
    """A filter applied to one element, by name and parameters."""

    kind: str
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Transition:
    kind: str = "fade"
    duration_sec: float = 0.4


# --- sound (§4.4) ------------------------------------------------------------


@dataclass(frozen=True)
class ElementAudio:
    """One structure for every sound an element can make.

    Music is an element with `ducked_by_speech` and `loop`; a sound effect is a
    short one with a natural duration; a voiceover is one that ducks everything
    else. Today those are three separate entities that share most of their
    fields — `MusicBed`, `SoundEffect`, and the insert audio that was thrown
    away — and having three is why adding a fourth meant a fourth code path.
    """

    enabled: bool = False
    gain_db: Animated = field(default_factory=lambda: Animated(0.0))
    duck_others_db: float = 0.0
    ducked_by_speech: bool = False
    fade_in_sec: float = 0.0
    fade_out_sec: float = 0.0
    loop: bool = False


# --- elements, rules, tracks (§4, §5) ----------------------------------------


@dataclass(frozen=True)
class Element:
    id: str
    slot: Slot = field(default_factory=Slot)
    start: Anchor = field(default_factory=Anchor)
    duration: Duration = field(default_factory=Duration)
    frame: Frame = field(default_factory=Frame)
    effects: tuple[Effect, ...] = ()
    audio: ElementAudio = field(default_factory=ElementAudio)
    transition_in: Transition | None = None
    transition_out: Transition | None = None
    label: str = ""
    # Whether this element may be dropped when the spine does not fit. An
    # outro is; the source is not. Lowest priority goes first.
    optional: bool = False
    priority: int = 0

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("every element needs an id")


RULE_KEYWORD_BROLL = "keyword_broll"     # today's inserts.choose_inserts
RULE_ON_EVERY_CUT = "on_every_cut"       # today's audio.choose_effects
RULE_CADENCE = "cadence"
RULE_ON_LOUDEST = "on_loudest"
RULES = (RULE_KEYWORD_BROLL, RULE_ON_EVERY_CUT, RULE_CADENCE, RULE_ON_LOUDEST)


@dataclass(frozen=True)
class RuleElement:
    """Not one element but a description of how to make several.

    Half the value of the present pipeline is that it places b-roll by itself,
    and that cannot become a separate mechanism bolted beside the scenario —
    the operator has to see the automation as part of the montage. The limits
    are the ones the README earned the hard way: never over the hook, no more
    than one every six seconds, never more than a third of the clip.
    """

    id: str
    rule: str
    params: Mapping[str, Any] = field(default_factory=dict)
    template: Element | None = None
    limit: int = 4
    min_gap_sec: float = 6.0
    guard_head_sec: float = 2.5
    guard_tail_sec: float = 1.5
    max_share: float = 0.35

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("every rule needs an id")
        if self.rule not in RULES:
            raise ValueError(f"unknown rule {self.rule!r}; expected one of {RULES}")


TRACK_SPINE = "spine"
TRACK_VIDEO = "video"
TRACK_AUDIO = "audio"
TRACK_OVERLAY = "overlay"
TRACK_KINDS = (TRACK_SPINE, TRACK_VIDEO, TRACK_AUDIO, TRACK_OVERLAY)


@dataclass(frozen=True)
class Track:
    id: str
    kind: str = TRACK_VIDEO
    z: int = 0
    elements: tuple[Element | RuleElement, ...] = ()
    muted: bool = False
    locked: bool = False

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("every track needs an id")
        if self.kind not in TRACK_KINDS:
            raise ValueError(f"unknown track kind {self.kind!r}; expected one of {TRACK_KINDS}")


@dataclass(frozen=True)
class MockClip:
    """The imaginary source the editor draws.

    Takes no part in rendering at all. It exists so the editor has something to
    show, and so the operator can drop the mock to 30 seconds and watch the
    montage re-lay itself — which is the only honest way to see whether a
    scenario survives a short clip.
    """

    duration_sec: float = 90.0
    width: int = 1920
    height: int = 1080
    cuts: tuple[float, ...] = ()
    sample_clip_id: int | None = None


@dataclass(frozen=True)
class Scenario:
    """A montage, as a pure value."""

    name: str
    tracks: tuple[Track, ...] = ()
    canvas: Canvas = field(default_factory=Canvas)
    style: StyleSpec = field(default_factory=StyleSpec.from_settings)
    mock: MockClip = field(default_factory=MockClip)
    version: int = VERSION

    def __post_init__(self) -> None:
        spines = [track for track in self.tracks if track.kind == TRACK_SPINE]
        if len(spines) > 1:
            raise ValueError("a scenario has exactly one spine, not several")
        ids = [element.id for track in self.tracks for element in track.elements]
        duplicates = {name for name in ids if ids.count(name) > 1}
        if duplicates:
            raise ValueError(f"element ids must be unique; repeated: {sorted(duplicates)}")

    @property
    def spine(self) -> Track | None:
        """The track that decides how long the clip is.

        Exactly one, and everything else is an overlay anchored to it. That is
        the same split `Composition` already draws between `segments` and
        `inserts`, and it is what guarantees adding a picture cannot shift the
        subtitles underneath it.
        """
        return next((track for track in self.tracks if track.kind == TRACK_SPINE), None)

    @property
    def elements(self) -> tuple[Element | RuleElement, ...]:
        return tuple(element for track in self.tracks for element in track.elements)

    def track_of(self, element_id: str) -> Track | None:
        return next(
            (t for t in self.tracks if any(e.id == element_id for e in t.elements)), None
        )
