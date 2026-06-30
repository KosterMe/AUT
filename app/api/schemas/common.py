"""Schema pieces shared across resources."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.core.clock import isoformat_z

# Read models come straight from ORM rows, and every timestamp leaves as UTC
# with a trailing Z — the database stores naive UTC, so without this the
# frontend would parse them as local time.
ORM = ConfigDict(from_attributes=True)


class UtcTimestamps(BaseModel):
    """Mixin that serializes the standard timestamp fields as ISO-8601 Z."""

    @field_serializer("created_at", "updated_at", check_fields=False)
    def _serialize_timestamps(self, value: datetime | None) -> str | None:
        return isoformat_z(value)


class UploadOptions(BaseModel):
    """TikTok's per-post switches.

    `visibility_type=1` (private) is rejected when scheduling: TikTok will not
    accept a scheduled private post, and finding out at publish time wastes
    the slot.
    """

    allow_comment: Literal[0, 1] = 1
    allow_duet: Literal[0, 1] = 0
    allow_stitch: Literal[0, 1] = 0
    visibility_type: Literal[0, 1] = 0
    brand_organic_type: Literal[0, 1] = 0
    branded_content_type: Literal[0, 1] = 0
    ai_label: Literal[0, 1] = 0
    proxy: str = ""


class Message(BaseModel):
    ok: bool = True
    message: str = ""


class Page(BaseModel):
    limit: int = Field(default=100, ge=1, le=500)
    offset: int = Field(default=0, ge=0)
