from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_serializer

from app.api.schemas.common import ORM, UtcTimestamps
from app.core.clock import isoformat_z

# TikTok usernames; the pattern also keeps the value safe as a filename, since
# it becomes part of the cookie file's name.
USERNAME_PATTERN = r"^[A-Za-z0-9_.\-]+$"


class AccountRead(UtcTimestamps):
    model_config = ORM

    id: int
    username: str
    display_name: Optional[str]
    has_valid_session: bool
    created_at: datetime
    updated_at: datetime
    last_used_at: Optional[datetime]

    @field_serializer("last_used_at")
    def _serialize_last_used(self, value: datetime | None) -> str | None:
        return isoformat_z(value)


class AccountCreate(BaseModel):
    username: str = Field(min_length=1, max_length=128, pattern=USERNAME_PATTERN)
    display_name: Optional[str] = Field(default=None, max_length=128)


class AccountUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=128)
