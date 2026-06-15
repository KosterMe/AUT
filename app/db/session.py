"""Engine and session management.

One engine per process, created lazily from settings. SQLite gets WAL mode so
the API and several worker processes can share the file; the same code runs on
PostgreSQL by setting APP_DATABASE_URL, which is the intended path once one
machine is no longer enough.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.core.config import get_settings

log = logging.getLogger(__name__)

_engine: Engine | None = None


def _create_engine(url: str) -> Engine:
    settings = get_settings()
    is_sqlite = url.startswith("sqlite")

    if is_sqlite:
        # SQLite will not create a missing parent directory; it just reports
        # "unable to open database file", which is a confusing first-run error.
        target = url.replace("sqlite:///", "", 1)
        if target and target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)

    kwargs: dict = {"echo": settings.db.echo_sql, "future": True}
    if is_sqlite:
        # FastAPI runs sync handlers in a threadpool and workers use threads;
        # each still gets its own Session, so sharing the connection across
        # threads is safe here and required for the pool to work at all.
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # Long-lived worker processes outlive server-side connection timeouts.
        kwargs["pool_pre_ping"] = True

    engine = create_engine(url, **kwargs)

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _record):  # pragma: no cover - driver callback
            cur = dbapi_conn.cursor()
            # WAL lets readers work while a writer holds the file.
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.execute(f"PRAGMA busy_timeout={settings.db.busy_timeout_ms};")
            # Off by default in SQLite; without it the clip -> publication
            # foreign keys would not actually be enforced.
            cur.execute("PRAGMA foreign_keys=ON;")
            cur.close()

    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = get_settings().database_url
        log.debug("creating engine for %s", url)
        _engine = _create_engine(url)
    return _engine


def set_engine(engine: Engine | None) -> None:
    """Replace the process engine. Used by tests; nothing else should call it."""
    global _engine
    _engine = engine


def create_all() -> None:
    """Create missing tables.

    Convenience for tests and first-run local use. Deployments run Alembic —
    see `alembic upgrade head` in the container entrypoints.
    """
    from app.db import models  # noqa: F401  (register tables on SQLModel.metadata)

    SQLModel.metadata.create_all(get_engine())


def new_session() -> Session:
    """A session that keeps loaded attributes readable after commit.

    `expire_on_commit=False` matters here: nearly every write is immediately
    followed by reading the row back (to return it from an endpoint, or to log
    what a worker just did). With the default, each of those reads triggers a
    fresh SELECT — or a DetachedInstanceError once the session has closed.
    Code that genuinely needs to see another transaction's writes calls
    `session.expire_all()` explicitly; the queue's claim does exactly that.
    """
    return Session(get_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session: commits on success, rolls back on failure.

    Services and task handlers use this. Leaving commit/rollback to the caller
    is what let the previous version half-write a job status and then raise.
    """
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency — one session per request, committed by the caller.

    Kept separate from `session_scope` because FastAPI needs a generator and
    because read endpoints should not implicitly commit.
    """
    with new_session() as session:
        yield session
