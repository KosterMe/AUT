from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.api.schemas.common import ORM, UtcTimestamps
from app.domain import profiles, sources


class RenderOptions(BaseModel):
    """How clips of a job are rendered.

    The switches that a profile also decides are `None` by default, and `None`
    means "whatever the profile says" rather than "off". Without that
    distinction a request that simply did not mention b-roll would turn it off
    for the profile whose whole point is having it.
    """

    width: int = Field(default=1080, ge=360, le=2160)
    height: int = Field(default=1920, ge=640, le=3840)
    crf: int = Field(default=23, ge=16, le=35)
    subtitle_font_size: int = Field(default=82, ge=24, le=120)
    subtitle_position_percent: int = Field(default=76, ge=50, le=88)

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
    # How the composition is compiled. None follows AUTOCLIPS_RENDER_STRATEGY;
    # "two_stage" is worth forcing while iterating, when the same clips are
    # rendered repeatedly and their segments can be reused.
    strategy: Optional[Literal["auto", "one_pass", "two_stage"]] = None


class ClipJobCreate(BaseModel):
    source_ref: str = Field(min_length=1, max_length=2048)
    source_platform: Literal["auto", "youtube", "local"] = "auto"
    title: Optional[str] = Field(default=None, max_length=512)
    # Manual name burned on clips and used as the caption's first line.
    custom_title: Optional[str] = Field(default=None, max_length=200)
    # When set, these tags are used exclusively for every clip of this video.
    caption_tags: Optional[str] = Field(default=None, max_length=500)

    # What kind of video this is; it picks the cutter and the frame.
    profile: Literal["talking", "plain", "split", "film"] = profiles.DEFAULT

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

    # Omitted keeps the profile the job was created with.
    profile: Optional[Literal["talking", "plain", "split", "film"]] = None
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
