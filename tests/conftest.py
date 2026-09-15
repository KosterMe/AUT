"""Shared test fixtures.

The most important thing here is isolation from the developer's machine. The
previous suite read the real `.env` at import time, so two subtitle tests
failed on this checkout purely because `AUTOCLIPS_SUBTITLE_UPPERCASE=1` was
set locally. Tests now start from a known-empty configuration and opt into
whatever they need.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlmodel import Session

from app.core.config import get_settings
from montage.config import get_settings as get_montage_settings
from app.db import session as db_session

# Every prefix the application reads configuration from. Anything matching is
# removed before a test runs, so local tuning cannot change test outcomes.
_CONFIG_PREFIXES = ("APP_", "QUEUE_", "TIKTOK_", "YTDLP_", "AUTOCLIPS_")


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Blank configuration + a private database and media tree per test."""
    for key in list(os.environ):
        if key.startswith(_CONFIG_PREFIXES):
            monkeypatch.delenv(key, raising=False)

    data_dir = tmp_path / "data"
    monkeypatch.setenv("APP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("APP_COOKIES_DIR", str(tmp_path / "cookies"))
    monkeypatch.setenv("APP_VIDEOS_DIR", str(tmp_path / "videos"))
    monkeypatch.setenv("AUTOCLIPS_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("APP_DATABASE_URL", f"sqlite:///{(data_dir / 'test.db').as_posix()}")
    # Keep the queue snappy; individual tests override what they exercise.
    monkeypatch.setenv("QUEUE_POLL_SECONDS", "0.01")
    monkeypatch.setenv("QUEUE_HEARTBEAT_SECONDS", "0.05")
    monkeypatch.setenv("QUEUE_LEASE_SECONDS", "60")

    for directory in (data_dir, tmp_path / "cookies", tmp_path / "videos", tmp_path / "media"):
        directory.mkdir(parents=True, exist_ok=True)

    # Cleared, not populated: a test that sets its own env vars before touching
    # settings must see them. Building the Settings object here would cache it
    # first and silently ignore whatever the test configured.
    #
    # Both of them: AUT and the montage service read the same environment into
    # two caches of their own, and clearing one leaves the other answering with
    # whatever the previous test configured.
    get_settings.cache_clear()
    get_montage_settings.cache_clear()
    db_session.set_engine(None)

    yield

    db_session.set_engine(None)
    get_settings.cache_clear()
    get_montage_settings.cache_clear()


@pytest.fixture()
def db() -> Iterator[Session]:
    """A session against a freshly created schema."""
    db_session.create_all()
    with Session(db_session.get_engine()) as session:
        yield session


@pytest.fixture()
def settings():
    return get_settings()


@pytest.fixture()
def configure(monkeypatch: pytest.MonkeyPatch):
    """Set configuration env vars and rebuild the cached Settings.

    Settings are cached per process, so changing the environment without this
    has no effect once anything has read them. Both caches: AUT and the montage
    service read the same `AUTOCLIPS_*` variables into separate Settings of
    their own, and rebuilding one leaves the other answering from before.
    """

    def apply(**env: object) -> None:
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
        get_settings.cache_clear()
        get_montage_settings.cache_clear()

    return apply
