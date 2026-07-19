"""Recognising what kind of source the user gave us.

Pure string work, kept in the domain because both the API (validating a
request) and the workers (deciding how to fetch) need the same answer, and
they must never disagree about it.
"""
from __future__ import annotations

import re

from app.db.enums import SourceKind  # noqa: F401  (re-exported for callers)

_YOUTUBE_URL = re.compile(
    r"^https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?v=|shorts/|embed/|live/)|youtu\.be/)[\w\-]+",
    re.IGNORECASE,
)
_YOUTUBE_HOST = re.compile(r"(?:^|//|\.)(?:youtube\.com|youtu\.be)/", re.IGNORECASE)

PLATFORM_YOUTUBE = "youtube"
PLATFORM_LOCAL = "local"


def is_youtube_url(value: str) -> bool:
    """Strict check, used to reject bad input at the API boundary."""
    return bool(_YOUTUBE_URL.match(value.strip()))


def infer_platform(source_ref: str) -> str:
    """Loose check, used to route work once the input is already accepted.

    Deliberately more permissive than `is_youtube_url`: by this point the
    reference has been validated, and a playlist or timestamped link should
    still be handled by the YouTube path rather than treated as a file.
    """
    return PLATFORM_YOUTUBE if _YOUTUBE_HOST.search(source_ref.strip()) else PLATFORM_LOCAL


def is_remote(source_ref: str) -> bool:
    return infer_platform(source_ref) != PLATFORM_LOCAL
