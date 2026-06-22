"""What a task handler is given to work with.

Handlers receive a `TaskContext` and nothing else. They report progress and
check for cancellation through it, and they open their own short database
sessions when they need one.

That last point is deliberate. The previous version passed one `Session` into
`slicer.slice_job` and held it open across a download and a full Whisper
transcription — minutes of a write transaction on SQLite, blocking the API.
Here, a handler that wants the database opens a session for the few
milliseconds it needs.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session

from app.core.errors import TaskCancelled
from app.db.session import session_scope
from app.tasks import queue

log = logging.getLogger(__name__)


@dataclass
class TaskContext:
    task_id: int
    kind: str
    payload: dict[str, Any]
    attempt: int = 1
    _last_stage: str = field(default="", init=False, repr=False)

    @contextmanager
    def db(self) -> Iterator[Session]:
        """A short-lived transactional session."""
        with session_scope() as session:
            yield session

    def progress(self, stage: str, value: float) -> None:
        """Publish a step and its completion fraction, then honour cancellation.

        Reporting progress is the natural place to check for a cancel request:
        handlers already call it between meaningful steps, so cancellation
        takes effect promptly without them writing any extra code.
        """
        if stage != self._last_stage:
            log.info("task %s: %s", self.task_id, stage)
            self._last_stage = stage
        with session_scope() as session:
            queue.set_progress(session, self.task_id, stage, value)
        self.raise_if_cancelled()

    def raise_if_cancelled(self) -> None:
        with session_scope() as session:
            if queue.is_cancel_requested(session, self.task_id):
                raise TaskCancelled("cancelled by user")

    def mark_irreversible(self) -> None:
        """Call immediately before a side effect that cannot be repeated.

        After this the queue will never auto-retry the task, even if the
        worker is killed mid-step — a duplicate TikTok post is worse than a
        job that needs a human glance.
        """
        with session_scope() as session:
            queue.mark_irreversible(session, self.task_id)

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)

    def require_int(self, key: str) -> int:
        value = self.payload.get(key)
        if value is None:
            raise ValueError(f"task {self.task_id} payload is missing '{key}'")
        return int(value)
