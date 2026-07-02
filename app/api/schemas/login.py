from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Union

from pydantic import BaseModel, Field, field_serializer

from app.api.schemas.accounts import USERNAME_PATTERN
from app.api.schemas.common import ORM
from app.core.clock import isoformat_z


class LoginSessionRead(BaseModel):
    model_config = ORM

    id: str
    username: str
    method: str
    status: str
    error: Optional[str]
    started_at: datetime
    completed_at: Optional[datetime]

    @field_serializer("started_at", "completed_at")
    def _serialize_times(self, value: datetime | None) -> str | None:
        return isoformat_z(value)


class CookieImportRequest(BaseModel):
    """Cookies exported from a browser already signed in to TikTok.

    `cookies` takes either the JSON array a cookie-export extension produces or
    the raw text of a Netscape `cookies.txt`; the format is detected.
    """

    username: str = Field(min_length=1, max_length=128, pattern=USERNAME_PATTERN)
    cookies: Union[str, list[dict[str, Any]]]


class LocalBrowserLoginRequest(BaseModel):
    """Open Chrome on the machine running the API. Desktop only."""

    username: str = Field(min_length=1, max_length=128, pattern=USERNAME_PATTERN)
