"""What kind of video this is, and therefore how to cut and dress it.

A profile is one decision the operator makes — "this is a podcast", "this is a
film" — that fixes four others they would otherwise have to make per job: which
cutter runs, how the frame is filled, whether b-roll is laid over it, and
whether silence is cut out. Those four are not independent. Scene cutting with
karaoke subtitles timed to speech that is not there produces nothing; a split
screen under a clip that already has b-roll over it produces a mess.

It used to fix a fifth: whether the source got transcribed. That one was the
odd member of the set, because it is not a statement about the material at all
— it is what one particular cutter needs in order to run. Having it here meant
a profile chosen for its subtitles decided what the cutting stage did, and a
talking profile pointed at the scene cutter transcribed a whole source nothing
went on to read. It lives with the cutters now: `cutting.needs_transcript`.

Profiles are values, not configuration: they are the vocabulary the API and the
handlers share, and every one of them can still be overridden field by field
through `RenderOptions` when a particular video needs it.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.cutting import CUTTER_SCENES, CUTTER_SPEECH, CUTTERS
from app.domain.style import (
    BACKGROUND_TAG,
    LAYOUT_AUTO,
    LAYOUT_SPLIT,
    PLANNABLE_LAYOUTS,
)


@dataclass(frozen=True)
class Profile:
    """One content type, and the four decisions that follow from it."""

    name: str
    cutter: str
    # "auto" leaves the choice between a blurred backdrop and a plain crop to
    # the source's own shape, which is only known once a file is on disk.
    layout: str
    remove_silence: bool
    inserts: bool
    # A music bed under the clip, and transition sounds on its cuts. Both need
    # a library asset tagged `music` or `sfx`, so both are no-ops until there
    # is one — which is why they can be on by default without changing what
    # anybody's existing library produces.
    music: bool = False
    sfx: bool = False
    burn_subtitles: bool = True
    # Split screen only: the library tag whose assets fill the bottom half.
    companion_tag: str = ""
    summary: str = ""

    def __post_init__(self) -> None:
        if self.cutter not in CUTTERS:
            raise ValueError(f"unknown cutter {self.cutter!r}; expected one of {CUTTERS}")
        if self.layout not in PLANNABLE_LAYOUTS:
            raise ValueError(
                f"unknown layout {self.layout!r}; expected one of {PLANNABLE_LAYOUTS}"
            )
        if self.layout == LAYOUT_SPLIT and not self.companion_tag:
            raise ValueError("a split-screen profile needs a companion tag")


TALKING = Profile(
    name="talking",
    cutter=CUTTER_SPEECH,
    layout=LAYOUT_AUTO,
    remove_silence=True,
    inserts=True,
    music=True,
    sfx=True,
    summary="Podcasts and interviews: cut on sentence ends, pauses removed, b-roll over the talk.",
)

PLAIN = Profile(
    name="plain",
    cutter=CUTTER_SPEECH,
    layout=LAYOUT_AUTO,
    remove_silence=False,
    inserts=False,
    summary="The same cuts, nothing added and nothing taken out. What to use when the "
            "material is already edited.",
)

SPLIT = Profile(
    name="split",
    cutter=CUTTER_SPEECH,
    layout=LAYOUT_SPLIT,
    remove_silence=True,
    inserts=False,
    music=True,
    sfx=True,
    companion_tag=BACKGROUND_TAG,
    summary="The speaker on top, filler footage below. No b-roll: the bottom half is "
            "already carrying the eye.",
)

FILM = Profile(
    name="film",
    cutter=CUTTER_SCENES,
    layout=LAYOUT_AUTO,
    remove_silence=False,
    inserts=False,
    summary="Films, shows and sport: cut where the picture cuts, ranked by loudness. "
            "Runs without a transcript, and keeps the score it came with.",
)

DEFAULT = TALKING.name

_BY_NAME = {profile.name: profile for profile in (TALKING, PLAIN, SPLIT, FILM)}
NAMES = tuple(_BY_NAME)


def get(name: str | None) -> Profile:
    """The named profile, or the default when nothing was asked for."""
    resolved = (name or DEFAULT).strip().lower()
    if resolved not in _BY_NAME:
        raise ValueError(f"unknown profile {name!r}; expected one of {NAMES}")
    return _BY_NAME[resolved]


def style_overrides(profile: Profile) -> dict:
    """The profile's opinions, in the shape a `StyleSpec` merges.

    Only the fields a profile actually has a view on. Everything else — the
    zoom, the grade, the type — is left to the layers underneath, which is
    what makes a profile a statement about the *material* rather than a
    complete look.
    """
    return {
        "framing": {"layout": profile.layout, "companion_tag": profile.companion_tag or None},
        "pacing": {"remove_silence": profile.remove_silence},
        "inserts": {"enabled": profile.inserts},
        "audio": {"music": profile.music, "sfx": profile.sfx},
        "subtitles": {"enabled": profile.burn_subtitles},
    }


def resolve(profile: Profile, overrides: dict | None = None) -> dict:
    """The profile's decisions, with any explicit overrides applied.

    An override of `None` means "not asked for" rather than "off" — which is
    why these fields are optional in the API schema. Without that distinction a
    request that simply did not mention b-roll would silently turn it off for a
    profile whose whole point is having it.
    """
    settings = {
        "profile": profile.name,
        "cutter": profile.cutter,
        "layout": profile.layout,
        "auto_montage": profile.remove_silence,
        "inserts": profile.inserts,
        "music": profile.music,
        "sfx": profile.sfx,
        "burn_subtitles": profile.burn_subtitles,
        "companion_tag": profile.companion_tag,
    }
    for key, value in (overrides or {}).items():
        if value is not None and key in settings:
            settings[key] = value
    return settings


__all__ = [
    "BACKGROUND_TAG",
    "DEFAULT",
    "FILM",
    "NAMES",
    "PLAIN",
    "Profile",
    "SPLIT",
    "TALKING",
    "get",
    "resolve",
    "style_overrides",
]
