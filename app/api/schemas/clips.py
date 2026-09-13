from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.api.schemas.common import ORM, UtcTimestamps
from app.domain import sources


class RenderOptions(BaseModel):
    """How clips of a job are rendered.

    Every field is `None` by default, and `None` means "whatever the style
    says" rather than "off". Without that distinction a request that simply did
    not mention b-roll would turn it off for the profile whose whole point is
    having it — and a form that always sent its own default frame size would
    quietly overrule every preset that asked for a different one.

    The flat fields here are a convenience for the common switches. `style`
    carries the rest of the look and wins over them, so a caller can send one,
    the other, or neither.
    """

    width: Optional[int] = Field(default=None, ge=360, le=2160)
    height: Optional[int] = Field(default=None, ge=640, le=3840)
    crf: Optional[int] = Field(default=None, ge=16, le=35)
    subtitle_font_size: Optional[int] = Field(default=None, ge=24, le=200)
    subtitle_position_percent: Optional[int] = Field(default=None, ge=20, le=95)

    # A partial style: any subset of framing / grade / subtitles / pacing /
    # inserts / audio / delivery. Everything left out follows the preset, the
    # profile, and then the defaults, in that order.
    style: Optional[dict[str, Any]] = None

    burn_subtitles: Optional[bool] = None
    # Cuts silent stretches out of the clip. On for the talking and split
    # profiles, off for material that is already edited.
    auto_montage: Optional[bool] = None
    # Automatic b-roll from the asset library, placed against the clip's own
    # subtitle cues. Does nothing while the library is empty.
    inserts: Optional[bool] = None
    # A music bed under the clip, ducked out of the way of speech, and
    # transition sounds on its cuts. Both need library assets tagged `music`
    # and `sfx` respectively.
    music: Optional[bool] = None
    sfx: Optional[bool] = None
    # How the frame is filled. "auto" crops a source that is already vertical
    # and gives everything else a blurred backdrop.
    layout: Optional[Literal["auto", "blur", "fill", "split"]] = None
    # Split screen only: which library tag fills the bottom half. Omitted uses
    # the profile's, which is `background`.
    companion_tag: Optional[str] = Field(default=None, max_length=64)
    # How the composition is compiled. None follows AUTOCLIPS_RENDER_STRATEGY;
    # "two_stage" is worth forcing while iterating, when the same clips are
    # rendered repeatedly and their segments can be reused.
    strategy: Optional[Literal["auto", "one_pass", "two_stage"]] = None

    def to_style_overrides(self) -> dict[str, Any]:
        """These options as a style override, strongest layer of the chain.

        The flat switches are folded into the same shape the rest of the style
        uses, so downstream there is one vocabulary rather than two. `style`
        is applied last and therefore wins where they overlap.
        """
        overrides: dict[str, Any] = {
            "framing": {"layout": self.layout, "companion_tag": self.companion_tag},
            "pacing": {"remove_silence": self.auto_montage},
            "inserts": {"enabled": self.inserts},
            "audio": {"music": self.music, "sfx": self.sfx},
            "subtitles": {
                "enabled": self.burn_subtitles,
                "font_size": self.subtitle_font_size,
                "position_percent": self.subtitle_position_percent,
            },
            "delivery": {
                "width": self.width,
                "height": self.height,
                "crf": self.crf,
                "strategy": self.strategy,
            },
        }
        for group, values in (self.style or {}).items():
            if isinstance(values, dict):
                overrides.setdefault(group, {}).update(values)
            else:
                overrides[group] = values
        return overrides


class ClipRenderRequest(BaseModel):
    """Render one clip again, optionally with a different look.

    Both fields are optional and, with neither, this repeats the render the
    clip already had — which is the useful thing to do after changing the
    b-roll library.
    """

    style_id: Optional[int] = None
    style: Optional[dict[str, Any]] = None


class ClipPreviewRequest(BaseModel):
    """A few seconds of a clip, rendered small, to look at a style.

    Short and low-resolution on purpose: the value of a preview is that it
    comes back before you have lost your train of thought, and everything a
    style decides — framing, type, pacing, b-roll — is visible in four seconds
    at half size.
    """

    at_sec: float = Field(default=0.0, ge=0.0)
    duration_sec: float = Field(default=4.0, ge=0.5, le=12.0)
    scale: float = Field(default=0.5, ge=0.2, le=1.0)
    style_id: Optional[int] = None
    style: Optional[dict[str, Any]] = None


class ClipJobCreate(BaseModel):
    source_ref: str = Field(min_length=1, max_length=2048)
    source_platform: Literal["auto", "youtube", "local"] = "auto"
    title: Optional[str] = Field(default=None, max_length=512)
    # Manual name burned on clips and used as the caption's first line.
    custom_title: Optional[str] = Field(default=None, max_length=200)
    # When set, these tags are used exclusively for every clip of this video.
    caption_tags: Optional[str] = Field(default=None, max_length=500)

    # Which montage to render with, by id. Omitted means the built-in the
    # profile names, which is what every job had before scenarios were stored.
    scenario_id: Optional[int] = None
    # Which signal the cuts follow. Omitted means the profile's.
    cutter: Optional[Literal["speech", "scenes", "plain"]] = None
    # Deprecated, accepted for one release: a profile said both of the above at
    # once, plus a default look. Sending one is logged.
    profile: Optional[Literal["talking", "plain", "split", "film"]] = None
    # A saved look. Omitted means the profile's own defaults, which is what an
    # unattended job gets and why nothing has to be chosen for one to run.
    style_id: Optional[int] = None

    start_immediately: bool = True
    min_clip_seconds: float = Field(default=90.0, ge=5.0, le=180.0)
    max_clip_seconds: float = Field(default=120.0, ge=10.0, le=300.0)
    gap_seconds: float = Field(default=1.0, ge=0.0, le=10.0)
    max_clips: int = Field(default=0, ge=0, le=500)
    render: RenderOptions = Field(default_factory=RenderOptions)

    @model_validator(mode="after")
    def _validate(self) -> "ClipJobCreate":
        if self.max_clip_seconds < self.min_clip_seconds:
            raise ValueError("max_clip_seconds must be >= min_clip_seconds")
        if self.source_platform == "youtube" and not sources.is_youtube_url(self.source_ref):
            raise ValueError("source_ref must be a YouTube URL when source_platform='youtube'")
        return self


class ClipJobStart(BaseModel):
    """Re-run planning for an existing job, optionally with new boundaries."""

    # Omitted keeps what the job was created with, for all three.
    scenario_id: Optional[int] = None
    cutter: Optional[Literal["speech", "scenes", "plain"]] = None
    # Deprecated, as on creation: it re-derives the cutter and the default look.
    profile: Optional[Literal["talking", "plain", "split", "film"]] = None
    # As does an omitted style.
    style_id: Optional[int] = None
    min_clip_seconds: Optional[float] = Field(default=None, ge=5.0, le=180.0)
    max_clip_seconds: Optional[float] = Field(default=None, ge=10.0, le=300.0)
    gap_seconds: Optional[float] = Field(default=None, ge=0.0, le=10.0)
    max_clips: int = Field(default=0, ge=0, le=500)
    render: RenderOptions = Field(default_factory=RenderOptions)

    @model_validator(mode="after")
    def _validate(self) -> "ClipJobStart":
        if (
            self.min_clip_seconds is not None
            and self.max_clip_seconds is not None
            and self.max_clip_seconds < self.min_clip_seconds
        ):
            raise ValueError("max_clip_seconds must be >= min_clip_seconds")
        return self


class ClipJobTitleUpdate(BaseModel):
    # Empty clears the override and falls back to the detected source title.
    custom_title: str = Field(default="", max_length=200)


class ClipJobTagsUpdate(BaseModel):
    # Empty clears the tags and falls back to auto-detected ones.
    caption_tags: str = Field(default="", max_length=500)


class ClipRead(UtcTimestamps):
    model_config = ORM

    id: int
    job_id: int
    index: int
    start_sec: float
    end_sec: float
    duration_sec: float
    title: Optional[str]
    text: Optional[str]
    video_path: Optional[str]
    cover_path: Optional[str]
    status: str
    error: Optional[str]
    created_at: datetime
    updated_at: datetime

    # Set when this clip already has a publication that is not cancelled. The
    # UI needs it to avoid offering "schedule" for a clip that is spoken for.
    publication_id: Optional[int] = None
    publication_status: Optional[str] = None


class ClipJobRead(UtcTimestamps):
    model_config = ORM

    id: int
    source_platform: str
    source_ref: str
    profile: str
    scenario_id: Optional[int] = None
    cutter: str = ""
    style_id: Optional[int] = None
    title: Optional[str]
    custom_title: Optional[str]
    caption_tags: Optional[str]
    duration_seconds: Optional[float]
    status: str
    stage: Optional[str]
    progress: float
    error: Optional[str]
    created_at: datetime
    updated_at: datetime


class ClipJobDetail(ClipJobRead):
    """A job together with its clips — what the job page needs in one request."""

    clips: list[ClipRead] = Field(default_factory=list)
