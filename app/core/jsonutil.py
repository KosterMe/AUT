"""JSON helpers for the columns that legitimately stay JSON.

After normalization only genuinely open-ended payloads remain as JSON text:
source metadata from yt-dlp, task payloads, and upload option bags. These two
functions are the only way to touch them — the previous version had four
copies of `_safe_json` that disagreed on edge cases.
"""
from __future__ import annotations

import json
from typing import Any


def dumps(value: Any) -> str:
    """Deterministic, human-readable JSON. Sorted keys keep DB diffs stable."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def loads_dict(value: str | None) -> dict[str, Any]:
    """Parse a JSON object, returning {} for null, blank, malformed, or
    non-object input. Stored JSON is never trusted to be well-formed."""
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def loads_list(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
