"""Scenarios over the wire.

`data` is the scenario itself — the same object `montage.scenario.store`
writes and reads, sent as JSON rather than as the string the column holds.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.api.schemas.common import UtcTimestamps


class ScenarioRead(UtcTimestamps):
    id: int
    name: str
    description: str = ""
    # Ships with the service: it cannot be deleted, and an edit makes a copy.
    builtin: bool = False
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
