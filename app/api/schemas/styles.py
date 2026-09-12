"""Named looks over the wire.

The shape that matters here is `data`: a *partial* style. A form with forty
controls sends back the two somebody changed, and the resolution chain fills
the rest in — which is why the API can offer this much configuration without
anything becoming required. Nothing in this file has a mandatory field except
a preset's name.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.api.schemas.common import ORM, UtcTimestamps
from app.domain import profiles

ProfileName = Optional[str]


class StyleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    # Which kind of material this look was written for. A hint for the UI, not
    # a restriction: any preset can be attached to any job.
    profile: ProfileName = None
    # The overrides. Any subset of the style; unknown keys are dropped.
    data: dict[str, Any] = Field(default_factory=dict)


class StyleUpdate(BaseModel):
    """Everything optional: `None` means "leave this as it is".

    `data`, when given, replaces the overrides rather than merging into them.
    Merging would make it impossible to remove one, which is how a field goes
    back to following the default.
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, max_length=500)
    profile: ProfileName = None
    data: Optional[dict[str, Any]] = None


class StyleRead(UtcTimestamps):
    model_config = ORM

    id: int
    name: str
    description: str
    profile: Optional[str]
    # What this preset overrides, and what those overrides come out as once
    # the defaults are applied. The UI needs both: the first to know which
    # controls are set, the second to show what the others will do.
    data: dict[str, Any] = Field(default_factory=dict)
    resolved: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class StyleDefaults(BaseModel):
    """What a profile renders as with no preset at all.

    The UI shows these as the placeholder values behind every empty control,
    so a form nobody fills in still describes exactly what will happen.
    """

    profile: str = profiles.DEFAULT
    style: dict[str, Any] = Field(default_factory=dict)
    profiles: list[str] = Field(default_factory=lambda: list(profiles.NAMES))
