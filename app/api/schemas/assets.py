from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_serializer, field_validator

from app.api.schemas.common import ORM, UtcTimestamps
from app.core.clock import isoformat_z


class AssetTagsUpdate(BaseModel):
    # Free text: commas, spaces and #hashes all separate tags, because the
    # obvious thing to paste is a hashtag list. Empty clears them, which leaves
    # the asset usable only by the cadence fallback.
    tags: str = Field(default="", max_length=500)


class AssetRead(UtcTimestamps):
    model_config = ORM

    id: int
    kind: str
    original_name: str
    tags: list[str] = Field(default_factory=list)
    duration_sec: Optional[float]
    width: Optional[int]
    height: Optional[int]
    size_bytes: int
    last_used_at: Optional[datetime]
    use_count: int
    created_at: datetime
    updated_at: datetime

    @field_validator("tags", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        if isinstance(value, str):
            return [tag for tag in (part.strip() for part in value.split(",")) if tag]
        return value

    @field_serializer("last_used_at")
    def _serialize_last_used(self, value: datetime | None) -> str | None:
        return isoformat_z(value)
