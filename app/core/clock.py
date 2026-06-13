"""Time handling.

One rule, applied everywhere: **the database stores naive UTC**, and every
value crossing the API boundary is timezone-aware UTC.

SQLite does not preserve tzinfo through SQLAlchemy DateTime columns, so an
aware datetime written to the DB comes back naive and silently compares wrong
against `datetime.now(timezone.utc)`. Converting at the two boundaries — write
with `utc_now()`, read with `as_utc()` — keeps that from ever happening.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utc_now() -> datetime:
    """Naive UTC 'now', ready to store. The single source of 'now'."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def aware_utc_now() -> datetime:
    """Timezone-aware UTC 'now', for comparisons against API input."""
    return datetime.now(timezone.utc)


def to_utc_naive(value: datetime) -> datetime:
    """Normalize any datetime to the naive-UTC form used in the database."""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def as_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to a value read from the database, for serialization."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def isoformat_z(value: datetime | None) -> str | None:
    """ISO-8601 with a trailing Z, which is what the frontend parses."""
    aware = as_utc(value)
    return None if aware is None else aware.isoformat().replace("+00:00", "Z")


def seconds_from_now(seconds: float) -> datetime:
    return utc_now() + timedelta(seconds=seconds)
