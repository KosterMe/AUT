"""How a clip looks, as one value that travels with it.

Until now the look was a property of the *process*. `get_settings()` was called
from inside the filter builders, so changing the zoom meant editing `.env` and
restarting the workers, two accounts could not have two looks, and a clip
re-rendered after an unrelated configuration change came out different from the
one it replaced. The knobs existed — about forty of them — they were just
attached to the wrong thing.

A `StyleSpec` is the whole look as data: framing, grade, subtitles, pacing,
b-roll, soundtrack, delivery. It is resolved once, when a clip is planned, and
then carried inside the `Composition`. Three things follow from that:

* **A render is reproducible.** Everything that shaped the picture is in the
  composition that produced it, so re-rendering it a month later cannot drift.
* **The fragment cache key is honest for free.** It hashes the style it was
  rendered with instead of enumerating settings by hand and hoping the list
  stayed complete.
* **Two jobs can look different.** Which is the entire point.

**Partial by default.** Nothing that overrides a style has to be complete:
`merged({"subtitles": {"font_size": 96}})` changes the font size and leaves the
other forty fields alone. That is what lets a preset be three lines, a profile
be five, and the UI send only the fields somebody actually touched. Unknown
keys are ignored rather than rejected, so a preset written against an older
version still loads after a field is renamed.

Resolution order, weakest first:

    code defaults  ->  environment  ->  profile  ->  preset  ->  per-job

The environment keeps its place because an existing `.env` should keep working;
what it no longer does is decide anything at render time.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Any, Mapping

from app.core.config import get_settings

log = logging.getLogger(__name__)

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

# Tag a library asset must carry to be usable as the bottom half of a split
# screen, or as the soundtrack. Not settings: they are the vocabulary an
# operator tags with, and renaming them would silently empty every library that
# used the old names.
BACKGROUND_TAG = "background"
MUSIC_TAG = "music"
SFX_TAG = "sfx"
# These three are instructions rather than vocabulary: they say what a fragment
# is *for*, not what it shows, which is why they are matched exactly and never
# by stem — "музыкант" is something a clip talks about, "музыка" is a job a file
# does. And the library is tagged in whatever language its owner thinks in, so
# each role answers to its Russian name too: an asset tagged "фон" was meant as
# a split-screen background and, before this, quietly became b-roll instead.
ROLE_TAG_SYNONYMS: dict[str, tuple[str, ...]] = {
    BACKGROUND_TAG: ("фон", "задник", "подложка"),
    MUSIC_TAG: ("музыка", "муз", "трек"),
    SFX_TAG: ("звук", "звуки", "эффект"),
}


def tag_aliases(tag: str) -> tuple[str, ...]:
    """Every spelling that means this tag, the tag itself first.

    Anything that is not one of the three roles is its own only spelling, so a
    custom companion tag keeps meaning exactly what it says.
    """
    wanted = (tag or "").strip().lower()
    if not wanted:
        return ()
    return (wanted, *ROLE_TAG_SYNONYMS.get(wanted, ()))


# ----------------------------------------------------------------- the machinery


class Group:
    """A block of a style: merges partial dictionaries, exports whole ones.

    Every group is a frozen dataclass of scalars, which is what makes the merge
    a dozen lines rather than a schema library. Values are coerced to the
    field's declared type, so a number that arrived from JSON as a string does
    not silently become one inside a filter graph.
    """

    def merged(self, data: Mapping[str, Any] | None) -> "Group":
        if not data:
            return self
        changes: dict[str, Any] = {}
        known = {field.name for field in fields(self)}  # type: ignore[arg-type]
        for key, value in data.items():
            if key not in known:
                log.debug("ignoring unknown style key %r on %s", key, type(self).__name__)
                continue
            if value is None:  # "not asked for", never "off"
                continue
            changes[key] = _coerce(value, current=getattr(self, key))
        return replace(self, **changes) if changes else self  # type: ignore[type-var]

    def to_dict(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}  # type: ignore[arg-type]

    def overrides_over(self, base: "Group") -> dict[str, Any]:
        """Only the fields where this differs from `base`."""
        return {
            field.name: getattr(self, field.name)
            for field in fields(self)  # type: ignore[arg-type]
            if getattr(self, field.name) != getattr(base, field.name)
        }


def _coerce(value: Any, *, current: Any) -> Any:
    """Best-effort conversion to whatever the field already holds.

    Annotations are strings under `from __future__ import annotations`, so the
    type of the current value is the more reliable signal — and every field in
    every group is a scalar with a default, so there is always one to read.
    """
    if isinstance(current, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        return int(round(float(value)))
    if isinstance(current, float):
        return float(value)
    if isinstance(current, str):
        return str(value)
    return value


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# -------------------------------------------------------------------- the groups


@dataclass(frozen=True)
class FramingStyle(Group):
    """How the source is fitted into a 9:16 frame."""

    # "auto" decides from the source's shape once a file is on disk.
    layout: str = LAYOUT_AUTO
    # How far the sharp layer is zoomed into the blurred backdrop. 1.0 fits the
    # whole source frame, which leaves a 16:9 clip 608px tall in a 1920px
    # canvas — a lot of blur. Past ~1.35 the crop starts cutting faces that sit
    # near the frame edge.
    zoom: float = 1.2
    # The backdrop is blurred at 1/divisor of the output size and scaled back
    # up. A heavy blur destroys the detail the downscale removed anyway, so
    # this is free speed: measured 21.4s -> 10.9s on a 40-second clip.
    blur_divisor: int = 4
    blur_radius: float = 24.0
    # How much wider than the canvas a source may be and still be cropped to
    # fill it rather than floated over a blurred copy of itself. At 0.15 a 9:16
    # phone video fills the frame and a 4:5 photo does not — cropping that one
    # would throw away a third of its width.
    fill_tolerance: float = 0.15
    # Split screen only: the library tag whose assets fill the bottom half.
    companion_tag: str = BACKGROUND_TAG

    def __post_init__(self) -> None:
        if self.layout not in PLANNABLE_LAYOUTS:
            raise ValueError(
                f"unknown layout {self.layout!r}; expected one of {PLANNABLE_LAYOUTS}"
            )
        object.__setattr__(self, "zoom", _clamp(self.zoom, 1.0, 2.0))
        object.__setattr__(self, "blur_divisor", int(_clamp(self.blur_divisor, 1, 8)))
        object.__setattr__(self, "blur_radius", _clamp(self.blur_radius, 1.0, 128.0))
        object.__setattr__(self, "fill_tolerance", _clamp(self.fill_tolerance, 0.0, 1.0))

    @classmethod
    def from_settings(cls) -> "FramingStyle":
        render = get_settings().render
        return cls(zoom=render.foreground_zoom, blur_divisor=render.blur_scale_divisor)


@dataclass(frozen=True)
class GradeStyle(Group):
    """The pass over the finished frame: contrast, colour, sharpness."""

    contrast: float = 1.04
    saturation: float = 1.08
    sharpen: bool = True
    # The unsharp mask's luma and chroma amounts. Sharpening is the quickest
    # trade of crispness for throughput there is: turning it off took a 40s
    # clip from 10.9s to 6.9s.
    sharpen_luma: float = 0.55
    sharpen_chroma: float = 0.25

    def __post_init__(self) -> None:
        object.__setattr__(self, "contrast", _clamp(self.contrast, 0.5, 2.0))
        object.__setattr__(self, "saturation", _clamp(self.saturation, 0.0, 3.0))
        object.__setattr__(self, "sharpen_luma", _clamp(self.sharpen_luma, 0.0, 3.0))
        object.__setattr__(self, "sharpen_chroma", _clamp(self.sharpen_chroma, 0.0, 3.0))

    @classmethod
    def from_settings(cls) -> "GradeStyle":
        return cls(sharpen=get_settings().render.sharpen)


@dataclass(frozen=True)
class SubtitleStyle(Group):
    """The karaoke overlay burned on top.

    The font defaults to the bundled display face rather than to a system one:
    the container has no fonts installed, and a condensed display face is most
    of what makes burned subtitles read as deliberate rather than as a default.
    """

    enabled: bool = True
    font: str = "Oswald"
    font_size: int = 82
    # Vertical placement as a percentage down the frame.
    position_percent: int = 76
    colour: str = "#FFFFFF"
    outline_colour: str = "#000000"
    # Outline and shadow as a fraction of the font size, so they stay in
    # proportion when the size changes.
    outline_ratio: float = 0.12
    shadow_ratio: float = 0.015
    shadow_opacity: float = 0.52
    uppercase: bool = False
    strip_punctuation: bool = True
    # Each cue pops in (scale + fade) instead of appearing flat.
    animate: bool = True
    # Whisper word timestamps can run late on fast speech; negative shows cues
    # earlier.
    time_offset_seconds: float = 0.0
    # Extra on-screen time when a pause follows the word.
    hold_seconds: float = 0.15
    end_hold_seconds: float = 0.35
    max_line_chars: int = 24

    def __post_init__(self) -> None:
        object.__setattr__(self, "font_size", int(_clamp(self.font_size, 24, 200)))
        object.__setattr__(self, "position_percent", int(_clamp(self.position_percent, 20, 95)))
        object.__setattr__(self, "outline_ratio", _clamp(self.outline_ratio, 0.0, 0.5))
        object.__setattr__(self, "shadow_ratio", _clamp(self.shadow_ratio, 0.0, 0.5))
        object.__setattr__(self, "shadow_opacity", _clamp(self.shadow_opacity, 0.0, 1.0))
        object.__setattr__(self, "max_line_chars", int(_clamp(self.max_line_chars, 8, 80)))

    @classmethod
    def from_settings(cls) -> "SubtitleStyle":
        configured = get_settings().subtitles
        return cls(
            font=configured.title_font.strip() or "Oswald",
            font_size=configured.font_size,
            position_percent=configured.position_percent,
            uppercase=configured.uppercase,
            strip_punctuation=configured.strip_punct,
            animate=configured.animate,
            time_offset_seconds=configured.time_offset_seconds,
        )


@dataclass(frozen=True)
class PacingStyle(Group):
    """Cutting the pauses out of a clip.

    The guard at the bottom is what stops the feature from doing damage on
    material it does not suit: if removing silence would take out almost
    nothing, or would leave less than half the clip standing, the whole
    montage is abandoned and the clip plays as one continuous piece.
    """

    remove_silence: bool = False
    # Level below which audio counts as silence. Absolute dB, which is why a
    # quiet source may need it raised.
    noise_db: float = -35.0
    min_silence_seconds: float = 0.55
    # Breathing room left on each side of a cut, so a word is never clipped.
    padding_seconds: float = 0.12
    min_segment_seconds: float = 1.2
    # A montage of more pieces than this is a stutter, not an edit.
    max_segments: int = 24
    min_removed_seconds: float = 0.8
    min_kept_share: float = 0.55

    def __post_init__(self) -> None:
        object.__setattr__(self, "noise_db", _clamp(self.noise_db, -90.0, 0.0))
        object.__setattr__(self, "min_silence_seconds", _clamp(self.min_silence_seconds, 0.05, 10.0))
        object.__setattr__(self, "padding_seconds", _clamp(self.padding_seconds, 0.0, 2.0))
        object.__setattr__(self, "min_segment_seconds", _clamp(self.min_segment_seconds, 0.1, 30.0))
        object.__setattr__(self, "max_segments", int(_clamp(self.max_segments, 1, 400)))
        object.__setattr__(self, "min_kept_share", _clamp(self.min_kept_share, 0.0, 1.0))


@dataclass(frozen=True)
class InsertPolicy(Group):
    """How much b-roll a clip may carry, and where it may go.

    Every number here is a guardrail rather than a target. The pipeline runs
    unattended, so the failure mode to design against is not "too little
    b-roll" — it is a clip whose hook is buried under a stock video three
    seconds in.
    """

    enabled: bool = True
    kind: str = INSERT_FULL
    max_inserts: int = 4
    min_seconds: float = 1.5
    max_seconds: float = 3.5
    # Space between inserts. Without it a paragraph dense in keywords turns
    # into a slideshow.
    min_gap_seconds: float = 6.0
    hook_guard_seconds: float = 2.5
    tail_guard_seconds: float = 1.5
    # Ceiling on how much of the clip may be covered, as a fraction.
    max_share: float = 0.35
    # A fallback for clips whose words matched nothing: place inserts on a
    # fixed beat instead. Off, because on it makes tags decorative — every
    # asset in the library lands on every clip regardless of what is being
    # said, which is how a QR code tagged "машина, мусор" ended up on twenty
    # consecutive clips of a podcast that never mentioned either.
    cadence_seconds: float = 12.0
    cadence_when_no_match: bool = False
    # Geometry of a picture-in-picture insert, as fractions of the canvas.
    pip_width_share: float = 0.46
    pip_margin_px: int = 48
    pip_top_share: float = 0.094

    def __post_init__(self) -> None:
        if self.kind not in INSERT_KINDS:
            raise ValueError(f"unknown insert kind {self.kind!r}; expected one of {INSERT_KINDS}")
        if self.min_seconds > self.max_seconds:
            raise ValueError("min_seconds must not exceed max_seconds")
        object.__setattr__(self, "max_inserts", int(_clamp(self.max_inserts, 0, 24)))
        object.__setattr__(self, "max_share", _clamp(self.max_share, 0.0, 0.9))
        object.__setattr__(self, "pip_width_share", _clamp(self.pip_width_share, 0.1, 1.0))
        object.__setattr__(self, "pip_top_share", _clamp(self.pip_top_share, 0.0, 0.9))

    @classmethod
    def from_settings(cls) -> "InsertPolicy":
        configured = get_settings().inserts
        return cls(
            enabled=configured.enabled,
            kind=configured.kind,
            max_inserts=configured.max_per_clip,
            min_seconds=configured.min_seconds,
            max_seconds=configured.max_seconds,
            min_gap_seconds=configured.min_gap_seconds,
            hook_guard_seconds=configured.hook_guard_seconds,
            tail_guard_seconds=configured.tail_guard_seconds,
            max_share=configured.max_share,
            cadence_seconds=configured.cadence_seconds,
            cadence_when_no_match=configured.cadence_when_no_match,
        )


@dataclass(frozen=True)
class AudioPolicy(Group):
    """The music bed and the transition sounds.

    Levels are relative. The finished mix is normalised to -14 LUFS at the end,
    so what these control is the balance between the bed and everything above
    it, not the loudness of the clip.
    """

    # Master switch, plus one per element: a profile turns music on without
    # having to know whether the library has any.
    enabled: bool = True
    music: bool = False
    sfx: bool = False

    music_gain_db: float = -20.0
    music_fade_in_sec: float = 0.6
    music_fade_out_sec: float = 1.2
    # Sidechain compression: the voice triggers, the music gets out of the way.
    duck_threshold: float = 0.03
    duck_ratio: float = 8.0
    duck_attack_ms: float = 20.0
    duck_release_ms: float = 300.0

    effect_gain_db: float = -8.0
    effect_seconds: float = 1.0
    max_effects: int = 6
    effect_min_gap_seconds: float = 4.0
    # Effects land slightly before the cut they belong to: a transition sound
    # that starts on the frame of the cut reads as late.
    effect_lead_seconds: float = 0.12
    effects_on_cuts: bool = True
    effects_on_inserts: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "duck_threshold", _clamp(self.duck_threshold, 0.0001, 1.0))
        object.__setattr__(self, "duck_ratio", max(1.0, self.duck_ratio))
        object.__setattr__(self, "max_effects", int(_clamp(self.max_effects, 0, 40)))

    @classmethod
    def from_settings(cls) -> "AudioPolicy":
        configured = get_settings().audio
        return cls(
            enabled=configured.enabled,
            music_gain_db=configured.music_gain_db,
            music_fade_in_sec=configured.music_fade_in_seconds,
            music_fade_out_sec=configured.music_fade_out_seconds,
            duck_threshold=configured.duck_threshold,
            duck_ratio=configured.duck_ratio,
            duck_attack_ms=configured.duck_attack_ms,
            duck_release_ms=configured.duck_release_ms,
            effect_gain_db=configured.effect_gain_db,
            effect_seconds=configured.effect_seconds,
            max_effects=configured.max_effects_per_clip,
            effect_min_gap_seconds=configured.effect_min_gap_seconds,
            effects_on_cuts=configured.effects_on_cuts,
            effects_on_inserts=configured.effects_on_inserts,
        )


@dataclass(frozen=True)
class DeliveryStyle(Group):
    """The finished file: its size, its rate, and how it is compiled."""

    width: int = 1080
    height: int = 1920
    fps: int = 30
    crf: int = 23
    # "" follows AUTOCLIPS_RENDER_STRATEGY; a named strategy overrides it.
    strategy: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "width", int(_clamp(self.width, 360, 2160)))
        object.__setattr__(self, "height", int(_clamp(self.height, 640, 3840)))
        object.__setattr__(self, "fps", int(_clamp(self.fps, 15, 60)))
        object.__setattr__(self, "crf", int(_clamp(self.crf, 0, 40)))

    @classmethod
    def from_settings(cls) -> "DeliveryStyle":
        render = get_settings().render
        return cls(
            width=render.width, height=render.height, fps=render.fps, crf=render.crf,
        )


# --------------------------------------------------------------------- the whole


@dataclass(frozen=True)
class StyleSpec:
    """Everything about how a clip looks, in one value."""

    framing: FramingStyle = FramingStyle()
    grade: GradeStyle = GradeStyle()
    subtitles: SubtitleStyle = SubtitleStyle()
    pacing: PacingStyle = PacingStyle()
    inserts: InsertPolicy = InsertPolicy()
    audio: AudioPolicy = AudioPolicy()
    delivery: DeliveryStyle = DeliveryStyle()
    version: int = VERSION

    @classmethod
    def from_settings(cls) -> "StyleSpec":
        """The defaults, with the environment's opinions folded in.

        This is the base every override sits on, and the reason an existing
        `.env` keeps working: the settings it holds still decide what a style
        starts as. What they no longer do is decide anything at render time.
        """
        return cls(
            framing=FramingStyle.from_settings(),
            grade=GradeStyle.from_settings(),
            subtitles=SubtitleStyle.from_settings(),
            pacing=PacingStyle(),
            inserts=InsertPolicy.from_settings(),
            audio=AudioPolicy.from_settings(),
            delivery=DeliveryStyle.from_settings(),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "StyleSpec":
        """A style from a stored dictionary, defaults filling every gap."""
        return cls.from_settings().merged(data)

    def merged(self, data: Mapping[str, Any] | None) -> "StyleSpec":
        """This style with a partial override applied.

        Partial at every level: a dictionary naming one field of one group
        leaves everything else exactly as it was. Anything else would make the
        UI responsible for round-tripping forty fields it never showed.
        """
        if not data:
            return self
        changes: dict[str, Any] = {}
        for name in _GROUP_NAMES:
            section = data.get(name)
            if isinstance(section, Mapping):
                merged = getattr(self, name).merged(section)
                if merged != getattr(self, name):
                    changes[name] = merged
            elif section is not None:
                log.debug("ignoring style section %r: expected an object", name)
        return replace(self, **changes) if changes else self

    def to_dict(self) -> dict[str, Any]:
        """The whole style, every field present. What the UI reads."""
        data: dict[str, Any] = {name: getattr(self, name).to_dict() for name in _GROUP_NAMES}
        data["version"] = self.version
        return data

    def overrides_over(self, base: "StyleSpec") -> dict[str, Any]:
        """Only what differs from `base`. What a preset stores.

        Presets keep the difference rather than the whole style on purpose: a
        preset that means "the default, but bigger subtitles" should still mean
        that after the default changes.
        """
        data: dict[str, Any] = {}
        for name in _GROUP_NAMES:
            section = getattr(self, name).overrides_over(getattr(base, name))
            if section:
                data[name] = section
        return data


_GROUP_NAMES: tuple[str, ...] = tuple(
    field.name for field in fields(StyleSpec) if is_dataclass(field.default)
)


def sanitise(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """A stored override dictionary, cleaned but not filled in.

    Unknown keys are dropped and values are coerced to the type the field
    holds, so what goes into the database is always loadable. What it is *not*
    is reduced to a difference from the defaults: a preset that says "do not
    remove silence" has to keep saying it even when that is what the defaults
    say anyway, because the profile layer underneath may well say the
    opposite, and a dropped field would let it win.

    Raises `ValueError` if the result is not a style anything could render —
    which is the point of calling it before a write rather than after.
    """
    if not data:
        return {}
    base = StyleSpec.from_settings()
    cleaned: dict[str, Any] = {}
    for name in _GROUP_NAMES:
        section = data.get(name)
        if not isinstance(section, Mapping):
            continue
        group = getattr(base, name)
        known = {field.name for field in fields(group)}
        kept = {
            key: _coerce(value, current=getattr(group, key))
            for key, value in section.items()
            if key in known and value is not None
        }
        if kept:
            cleaned[name] = kept
    base.merged(cleaned)  # raises if the result is not renderable
    return cleaned


def resolve(
    *,
    profile: Mapping[str, Any] | None = None,
    preset: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> StyleSpec:
    """The style for one clip, from weakest source to strongest.

    A layer that cannot be applied is skipped with a warning rather than
    failing the clip: a preset edited into an invalid state should cost the
    look it asked for, not the render. The pipeline runs unattended, and a job
    that fails at three in the morning because a number went out of range is
    worse than one that comes out with the default framing.
    """
    style = StyleSpec.from_settings()
    for label, layer in (("profile", profile), ("preset", preset), ("job", overrides)):
        if not layer:
            continue
        try:
            style = style.merged(layer)
        except (ValueError, TypeError) as error:
            log.warning("ignoring the %s style layer: %s", label, error)
    return style


__all__ = [
    "AudioPolicy",
    "BACKGROUND_TAG",
    "ROLE_TAG_SYNONYMS",
    "DeliveryStyle",
    "FramingStyle",
    "GradeStyle",
    "Group",
    "INSERT_FULL",
    "INSERT_KINDS",
    "INSERT_PIP",
    "InsertPolicy",
    "LAYOUTS",
    "LAYOUT_AUTO",
    "LAYOUT_BLUR",
    "LAYOUT_FILL",
    "LAYOUT_SPLIT",
    "MUSIC_TAG",
    "PLANNABLE_LAYOUTS",
    "PacingStyle",
    "SFX_TAG",
    "tag_aliases",
    "StyleSpec",
    "SubtitleStyle",
    "VERSION",
    "resolve",
    "sanitise",
]
