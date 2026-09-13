"""Scenarios over the wire.

`data` is the scenario itself — the same object `montage.scenario.store`
writes and reads, sent as JSON rather than as the string the column holds.

The inspect shapes below are the editor's, and they are flat on purpose: a
timeline draws blocks, and a block is a row with a start, a length and enough
about itself to be labelled and coloured. Everything they carry is *derived*
from one compile of the scenario — the editor decides nothing about where
anything goes, which is the only way it can be trusted to draw what will
actually be rendered.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.api.schemas.common import UtcTimestamps


class ScenarioRead(UtcTimestamps):
    id: int
    name: str
    # Which save this is. Send it back with an edit and a stale one is
    # refused instead of quietly winning.
    version: int = 1
    description: str = ""
    # Ships with the service: it cannot be deleted, and an edit makes a copy.
    builtin: bool = False
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ScenarioCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    # The whole scenario. Not sparse, unlike a style: there is no "the usual"
    # for a montage to be a difference from.
    data: dict[str, Any] = Field(default_factory=dict)


class ScenarioUpdate(BaseModel):
    """A saved edit. `data` replaces the scenario; the rest is optional.

    Editing a built-in saves a copy instead, so the response can come back
    with a different `id` than the one that was addressed — the editor is
    expected to follow it rather than keep writing to the original.
    """

    data: dict[str, Any]
    name: Optional[str] = Field(default=None, max_length=64)
    description: Optional[str] = Field(default=None, max_length=500)
    # The version this edit was made against. Omitted means "I did not look",
    # which a script writing a scenario has every right to say.
    version: Optional[int] = None


class ScenarioPreviewRequest(BaseModel):
    """«Примерить»: this scenario, on a real clip, small and fast.

    `data` previews an unsaved draft — what is on screen rather than what was
    last written down, which is what somebody pressing the button means.
    """

    clip_id: int
    data: Optional[dict[str, Any]] = None
    at_sec: float = Field(default=0.0, ge=0.0)
    duration_sec: float = Field(default=4.0, ge=0.5, le=12.0)
    scale: float = Field(default=0.5, ge=0.2, le=1.0)


class ScenarioInspectRequest(BaseModel):
    """Take apart what is on screen, which is not what was last saved.

    The editor asks this on every edit. It could save first and inspect the
    row, but then an editor would be a thing that writes to the database on
    every keystroke — and an unsaved draft is exactly the state somebody needs
    to see laid out before deciding whether to keep it.
    """

    data: dict[str, Any]
    duration_sec: Optional[float] = Field(default=None, ge=1.0, le=1800.0)
    # Where the playhead is. It matters only once something moves: a canvas
    # drawing a moving element where it starts, at every point of the
    # timeline, draws a frame that exists for one instant of the clip.
    at_sec: float = Field(default=0.0, ge=0.0)


class InspectRect(BaseModel):
    x: float
    y: float
    width: float
    height: float
    # Degrees, sampled at the report's moment like everything else here.
    rotate: float = 0.0
    fit: str
    # One frame of a moving rectangle. The editor draws a moving element from
    # here rather than from its own copy of the draft, which only knows where
    # it starts.
    moving: bool = False


class InspectKey(BaseModel):
    """One keyframe, at the second it resolved to on this clip."""

    property: str
    at_sec: float
    value: float
    easing: str = "linear"
    # How the key was written, so a key held to the end reads as held to the
    # end rather than as a number that happens to be large.
    anchor: str = "start"


class InspectBlock(BaseModel):
    element_id: str
    track_id: str
    track_kind: str
    label: str
    slot_kind: str
    slot_tag: str = ""
    at_sec: float
    duration_sec: float
    # What the block is held to — start, end, fraction, after, event — so the
    # timeline can show the intention and not only the outcome.
    anchor: str
    frame: InspectRect
    z: int = 0
    optional: bool = False
    # False means it was dropped: it did not fit this length, or nothing could
    # fill it. This is the mistake the layout switcher exists to catch.
    placed: bool = True
    note: str = ""
    keys: list[InspectKey] = Field(default_factory=list)


class InspectGhost(BaseModel):
    kind: str
    at_sec: float
    duration_sec: float
    source_path: str = ""


class InspectRule(BaseModel):
    element_id: str
    rule: str
    track_id: str
    label: str
    limit: int = 0
    # Where it fired on this material. Drawn dashed: on the next clip it will
    # be somewhere else.
    ghosts: list[InspectGhost] = Field(default_factory=list)


class InspectWarning(BaseModel):
    code: str
    message: str
    element_id: str = ""


class ScenarioInspect(BaseModel):
    """A scenario laid out on a clip of a given length that does not exist."""

    scenario_id: int
    name: str
    # Three lengths: what the clip offered, how long the scenario laid itself
    # out to be, and how long the file will be. The timeline is drawn in the
    # second; the third is what renders, and a gap between them means
    # something on the timeline does not reach the file yet — the warnings say
    # which.
    material_sec: float
    timeline_sec: float
    duration_sec: float
    canvas_width: int
    canvas_height: int
    layout: str
    blocks: list[InspectBlock] = Field(default_factory=list)
    rules: list[InspectRule] = Field(default_factory=list)
    warnings: list[InspectWarning] = Field(default_factory=list)
    subtitle_count: int = 0
    # The moment these rectangles are for.
    at_sec: float = 0.0
    # The lengths worth a button in the editor.
    durations: list[float] = Field(default_factory=list)
