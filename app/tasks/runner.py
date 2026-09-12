"""The worker loop.

`run_once` is the whole thing: reclaim, claim, execute, record. It returns
whether it did any work, which is what makes the worker testable — a test
drives it step by step instead of racing a background thread.

`run_forever` is the thin loop around it that a worker process runs.
"""
from __future__ import annotations

import logging
import threading
from typing import Sequence

from app.core.clock import utc_now
from app.core.config import get_settings
from app.core.errors import LeaseLost, PermanentError, TaskCancelled
from app.db.enums import TaskKind, TaskStatus
from app.db.models import Task
from app.db.session import session_scope
from app.tasks import queue
from app.tasks.context import TaskContext
from app.tasks.registry import get_handler

log = logging.getLogger(__name__)


class _Lease:
    """Refreshes a running task's lease until the handler returns.

    Without this a long render looks identical to a dead worker, and the
    reclaim sweep would take the task away from a process still doing it.
    """

    def __init__(self, task_id: int, interval: float):
        self._task_id = task_id
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Raised into the handler through the task context. The refresher used
        # to just stop when the lease was gone, leaving the handler running a
        # task another worker had already picked up: two renders writing the
        # same output file, or in the worst case two uploads of one video.
        self.lost = threading.Event()

    def __enter__(self) -> "_Lease":
        self._thread = threading.Thread(
            target=self._run, name=f"lease-{self._task_id}", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                with session_scope() as session:
                    if not queue.heartbeat(session, self._task_id):
                        # The task is no longer RUNNING under us: reclaimed
                        # after an expired lease, cancelled, or finalized.
                        # Tell the handler so it stops, and stop touching it.
                        self.lost.set()
                        log.warning(
                            "lease for task %s is gone; asking the handler to stop",
                            self._task_id,
                        )
                        return
            except Exception:  # pragma: no cover - best effort
                log.exception("lease refresh failed for task %s", self._task_id)


def run_once(kinds: Sequence[TaskKind | str] | None = None) -> bool:
    """Do at most one task. Returns True if a task was executed."""
    with session_scope() as session:
        reclaimed = [_snapshot(task) for task in queue.reclaim_expired(session)]
        task = queue.claim(session, kinds)
        snapshot = _snapshot(task) if task is not None else None

    # A task the sweep gave up on never ran its handler, so nothing has told
    # the entity behind it. Without this a publication whose worker was killed
    # mid-upload sits in "publishing" forever, and a clip stays "rendering".
    _notify_reclaimed(reclaimed)

    if snapshot is None:
        return False
    _execute(snapshot)
    return True


def _notify_reclaimed(tasks: list[_TaskSnapshot]) -> None:
    for task in tasks:
        if task.status not in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            continue  # requeued: it will run again, so nothing to report yet
        spec = get_handler(task.kind)
        if spec is None:
            continue
        ctx = TaskContext(task_id=task.id, kind=task.kind, payload=task.payload, attempt=task.attempts)
        _notify_failure(spec, ctx, task.error or "the worker stopped responding", final=True)


def run_forever(kinds: Sequence[TaskKind | str] | None = None, *, stop: threading.Event | None = None) -> None:
    settings = get_settings()
    stop = stop or threading.Event()
    label = ",".join(str(k) for k in kinds) if kinds else "all"
    log.info("worker started for kinds=[%s], poll=%ss", label, settings.queue.poll_seconds)

    while not stop.is_set():
        try:
            busy = run_once(kinds)
        except Exception:
            # A failure here is in the queue machinery itself, not a handler;
            # keep the worker alive so one bad row cannot stop the service.
            log.exception("worker cycle failed")
            busy = False
        # Back-to-back when there is a backlog, patient when there is not.
        stop.wait(0.2 if busy else settings.queue.poll_seconds)

    log.info("worker stopped")


def _execute(task: _TaskSnapshot) -> None:
    spec = get_handler(task.kind)
    if spec is None:
        with session_scope() as session:
            queue.finish_failure(
                session, task.id, f"no handler registered for kind '{task.kind}'", permanent=True
            )
        return

    settings = get_settings()
    lease = _Lease(task.id, settings.queue.heartbeat_seconds)
    ctx = TaskContext(
        task_id=task.id,
        kind=task.kind,
        payload=task.payload,
        attempt=task.attempts,
        lease_lost=lease.lost,
    )
    started = utc_now()

    try:
        with lease:
            result = spec.run(ctx) or {}
    except LeaseLost as exc:
        # Deliberately writes nothing: the task belongs to another worker now,
        # and its state is that worker's to report.
        log.warning("task %s (%s) abandoned: %s", task.id, task.kind, exc)
        return
    except TaskCancelled as exc:
        with session_scope() as session:
            queue.finish_cancelled(session, task.id, str(exc))
        _notify_failure(spec, ctx, str(exc), final=True)
        log.info("task %s (%s) cancelled", task.id, task.kind)
        return
    except PermanentError as exc:
        message = f"{type(exc).__name__}: {exc}"
        with session_scope() as session:
            queue.finish_failure(session, task.id, message, permanent=True)
        _notify_failure(spec, ctx, message, final=True)
        log.warning("task %s (%s) failed permanently: %s", task.id, task.kind, exc)
        return
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        with session_scope() as session:
            updated = queue.finish_failure(session, task.id, message)
            # Final only when the queue decided not to requeue it, so a hook
            # never marks an entity failed while a retry is still coming.
            final = updated is None or updated.status != TaskStatus.QUEUED
        _notify_failure(spec, ctx, message, final=final)
        log.exception("task %s (%s) failed%s", task.id, task.kind, "" if final else " (will retry)")
        return

    with session_scope() as session:
        queue.finish_success(session, task.id, result)
    log.info(
        "task %s (%s) succeeded in %.1fs",
        task.id,
        task.kind,
        (utc_now() - started).total_seconds(),
    )


def _notify_failure(spec, ctx: TaskContext, message: str, *, final: bool) -> None:
    """Let the handler record what its failure means for the entity behind it."""
    if spec.on_failure is None:
        return
    try:
        spec.on_failure(ctx, message, final)
    except Exception:  # pragma: no cover - a broken hook must not mask the failure
        log.exception("failure hook for task %s raised", ctx.task_id)


class _TaskSnapshot:
    """Plain data copied out of the claiming session.

    Handlers run for minutes; passing a live ORM object across that boundary
    would keep a session (and on SQLite, a connection) pinned the whole time.
    """

    __slots__ = ("id", "kind", "payload", "attempts", "status", "error")

    def __init__(self, id: int, kind: str, payload: dict, attempts: int,
                 status: str = "", error: str | None = None):
        self.id = id
        self.kind = kind
        self.payload = payload
        self.attempts = attempts
        self.status = status
        self.error = error


def _snapshot(task: Task) -> _TaskSnapshot:
    return _TaskSnapshot(
        id=int(task.id),
        kind=str(task.kind),
        payload=queue.payload_of(task),
        attempts=int(task.attempts),
        status=str(task.status),
        error=task.error,
    )
