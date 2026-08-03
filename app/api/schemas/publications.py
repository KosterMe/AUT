from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_serializer

from app.api.schemas.accounts import USERNAME_PATTERN
from app.api.schemas.common import ORM, UploadOptions, UtcTimestamps
from app.core.clock import isoformat_z


class PublicationRead(UtcTimestamps):
    model_config = ORM

    id: int
    account_id: int
    clip_id: Optional[int]
    source_kind: str
    source_ref: str
    caption: str
    scheduled_at: datetime
    status: str
    result_text: Optional[str]
    published_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    @field_serializer("scheduled_at", "published_at")
    def _serialize_times(self, value: datetime | None) -> str | None:
        return isoformat_z(value)


class ScheduleClipRequest(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    scheduled_at: datetime
    # Overrides the generated caption; leave unset to use the generated one.
    caption: Optional[str] = Field(default=None, min_length=1, max_length=2200)
    options: UploadOptions = Field(default_factory=UploadOptions)


class ScheduleJobRequest(BaseModel):
    """Queue every unscheduled clip of a job, spaced out in time."""

    username: str = Field(pattern=USERNAME_PATTERN)
    first_at: datetime
    interval_minutes: int = Field(default=60, ge=1, le=1440)
    options: UploadOptions = Field(default_factory=UploadOptions)


class ScheduleFileRequest(BaseModel):
    """Publish something that is not one of our clips."""

    username: str = Field(pattern=USERNAME_PATTERN)
    source_kind: Literal["local", "youtube"] = "local"
    source_ref: str = Field(min_length=1, max_length=2048)
    caption: str = Field(min_length=1, max_length=2200)
    scheduled_at: datetime
    options: UploadOptions = Field(default_factory=UploadOptions)


class PublicationUpdate(BaseModel):
    scheduled_at: Optional[datetime] = None
    caption: Optional[str] = Field(default=None, min_length=1, max_length=2200)
