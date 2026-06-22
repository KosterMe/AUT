"""Queue persistence: enqueue, claim, lease, finish, reclaim.

Every rule about how work is picked up and retried lives in this module, so
there is one implementation to reason about instead of the two divergent ones
the previous version carried.

Concurrency model
-----------------
A worker claims a task with a compare-and-swap: it reads a candidate, then
UPDATEs it only `WHERE id = ? AND status = 'queued'`. If `rowcount` is 0 some
other worker won the race and we move to the next candidate. This is correct
on SQLite and PostgreSQL alike, which is what keeps the door open to swapping
the backend without touching the queue.

A claimed task holds a **lease**. The worker refreshes it while working; if
the process dies the lease expires and `reclaim_expired` decides whether the
task can be retried (see `Task.irreversible`).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Iterable, Sequence

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.core.clock import utc_now
from app.core.config import get_settings
from app.core.jsonutil import dumps, loads_dict
from app.db.enums import TaskKind, TaskStatus
from app.db.models import Task

log = logging.getLogger(__name__)

# How many queued candidates to try before giving up a poll cycle. Bounded so
# a burst of contention cannot turn one poll into an unbounded scan.
_CLAIM_ATTEMPTS = 5


def enqueue(
    session: Session,
    kind: TaskKind | str,
    payload: dict[str, Any] | None = None,
    *,
    run_at=None,
    priority: int = 0,
    dedupe_key: str | None = None,
    max_attempts: int | None = None,
) -> Task:
    """Add a task, or return the existing one with the same `dedupe_key`.

    Deduplication is enforced by a unique index, not by a read-then-write
    check, so two API requests racing to schedule the same clip cannot both
    succeed.
    """
    settings = get_settings()

    if dedupe_key:
        existing = session.exec(select(Task).where(Task.dedupe_key == dedupe_key)).first()
        if existing is not None:
            # Only work that is still coming deduplicates. A task that already
            # finished must not block an equivalent one forever — otherwise a
            # failed download could never be restarted, and the caller would
            # get back the corpse of the old attempt with no way to tell.
            if TaskStatus(existing.status).is_active:
                return existing
            release_dedupe_key(session, existing)
            session.flush()

    task = Task(
        kind=str(kind),
        payload_json=dumps(payload or {}),
        status=TaskStatus.QUEUED,
        stage="queued",
        run_at=run_at or utc_now(),
        priority=priority,
        dedupe_key=dedupe_key,
        max_attempts=max_attempts if max_attempts is not None else settings.queue.max_attempts,
    )
    session.add(task)
    try:
        session.flush()
    except IntegrityError:
        # Lost the dedupe race: the winner's row is what the caller wants.
        session.rollback()
        existing = session.exec(select(Task).where(Task.dedupe_key == dedupe_key)).first()
        if existing is None:  # pragma: no cover - only if the conflict was elsewhere
            raise
        return existing
    return task


def release_dedupe_key(session: Session, task: Task) -> None:
    """Free a task's dedupe key so equivalent work can be queued again.

    Called when the intent behind a task is cancelled or explicitly retried.
    Without this, a failed publish would block its clip forever.
    """
    if task.dedupe_key is None:
        return
    task.dedupe_key = None
    task.updated_at = utc_now()
    session.add(task)


def claim(session: Session, kinds: Sequence[TaskKind | str] | None = None) -> Task | None:
    """Atomically take ownership of one due task, or return None if idle."""
    settings = get_settings()
    now = utc_now()
    lease_until = now + timedelta(seconds=settings.queue.lease_seconds)

    query = (
        select(Task)
        .where(Task.status == TaskStatus.QUEUED)
        .where(col(Task.cancel_requested) == False)  # noqa: E712
        .where(col(Task.run_at) <= now)
    )
    if kinds:
        query = query.where(col(Task.kind).in_([str(k) for k in kinds]))
    candidates = session.exec(
        query.order_by(col(Task.priority).desc(), col(Task.run_at)).limit(_CLAIM_ATTEMPTS)
    ).all()

    for candidate in candidates:
        result = session.exec(
            update(Task)
            .where(col(Task.id) == candidate.id)
            .where(col(Task.status) == TaskStatus.QUEUED)
            .where(col(Task.cancel_requested) == False)  # noqa: E712
            .values(
                status=TaskStatus.RUNNING,
                stage="running",
                attempts=Task.attempts + 1,
                heartbeat_at=now,
                lease_expires_at=lease_until,
                updated_at=now,
            )
        )
        session.commit()
        if result.rowcount == 1:
            session.expire_all()
            return session.get(Task, candidate.id)
    return None


def heartbeat(session: Session, task_id: int) -> bool:
    """Extend a running task's lease. Returns False if it is no longer ours."""
    settings = get_settings()
    now = utc_now()
    result = session.exec(
        update(Task)
        .where(col(Task.id) == task_id)
        .where(col(Task.status) == TaskStatus.RUNNING)
        .values(
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=settings.queue.lease_seconds),
            updated_at=now,
        )
    )
    session.commit()
    return result.rowcount == 1


def set_progress(session: Session, task_id: int, stage: str, progress: float) -> None:
    now = utc_now()
    session.exec(
        update(Task)
        .where(col(Task.id) == task_id)
        .values(stage=stage, progress=max(0.0, min(1.0, progress)), updated_at=now)
    )
    session.commit()


def mark_irreversible(session: Session, task_id: int) -> None:
    """Record that this task has passed the point where a retry is safe."""
    session.exec(
        update(Task).where(col(Task.id) == task_id).values(irreversible=True, updated_at=utc_now())
    )
    session.commit()


def is_cancel_requested(session: Session, task_id: int) -> bool:
    session.expire_all()
    task = session.get(Task, task_id)
    return bool(task and task.cancel_requested)


def finish_success(session: Session, task_id: int, result: dict[str, Any] | None = None) -> None:
    task = session.get(Task, task_id)
    if task is None:
        return
    task.status = TaskStatus.SUCCEEDED
    task.stage = "succeeded"
    task.progress = 1.0
    task.error = None
    task.result_json = dumps(result or {})
    _clear_lease(task)
    session.add(task)
    session.commit()


def finish_failure(
    session: Session,
    task_id: int,
    error: str,
    *,
    permanent: bool = False,
) -> Task | None:
    """Fail a task, or requeue it with backoff when attempts remain.

    `permanent=True` means retrying cannot help (bad input, dead session), so
    attempts are not burned waiting to find that out three times.
    """
    settings = get_settings()
    task = session.get(Task, task_id)
    if task is None:
        return None

    retryable = not permanent and not task.irreversible and task.attempts < task.max_attempts
    if retryable:
        task.status = TaskStatus.QUEUED
        task.stage = "retry_queued"
        # Linear backoff: a transient failure (rate limit, flaky network) needs
        # time to clear, and immediate retries burn every attempt in seconds.
        task.run_at = utc_now() + timedelta(
            seconds=settings.queue.retry_backoff_seconds * task.attempts
        )
    else:
        task.status = TaskStatus.FAILED
        task.stage = "failed"
        task.progress = 1.0
    task.error = error[:4096]
    _clear_lease(task)
    session.add(task)
    session.commit()
    return task


def finish_cancelled(session: Session, task_id: int, reason: str = "cancelled by user") -> None:
    task = session.get(Task, task_id)
    if task is None:
        return
    task.status = TaskStatus.CANCELLED
    task.stage = "cancelled"
    task.progress = 1.0
    task.error = reason
    _clear_lease(task)
    release_dedupe_key(session, task)
    session.add(task)
    session.commit()


def request_cancel(session: Session, task_id: int) -> Task | None:
    """Ask a task to stop.

    A queued task is cancelled outright. A running one only gets a flag: its
    handler checks it between steps, which is the only way to stop work that
    is already touching the filesystem or the network.
    """
    task = session.get(Task, task_id)
    if task is None or TaskStatus(task.status).is_terminal:
        return task

    task.cancel_requested = True
    task.updated_at = utc_now()
    if task.status == TaskStatus.QUEUED:
        task.status = TaskStatus.CANCELLED
        task.stage = "cancelled"
        task.progress = 1.0
        task.error = "cancelled by user"
        _clear_lease(task)
        release_dedupe_key(session, task)
    else:
        task.stage = "cancelling"
    session.add(task)
    session.commit()
    return task


def cancel_many(session: Session, task_ids: Iterable[int]) -> list[Task]:
    return [task for task in (request_cancel(session, tid) for tid in task_ids) if task]


def reclaim_expired(session: Session) -> list[Task]:
    """Recover tasks whose worker died, and finalize cancelled ones.

    Returns the tasks that changed so callers can react — a reclaimed render
    task, for example, has to put its clip back into a sane state.
    """
    now = utc_now()
    changed: list[Task] = []

    # A running task whose lease lapsed: its worker is gone.
    orphans = session.exec(
        select(Task)
        .where(Task.status == TaskStatus.RUNNING)
        .where(col(Task.lease_expires_at) != None)  # noqa: E711
        .where(col(Task.lease_expires_at) < now)
    ).all()

    for task in orphans:
        if task.cancel_requested:
            task.status = TaskStatus.CANCELLED
            task.stage = "cancelled"
            task.progress = 1.0
            task.error = task.error or "cancelled by user"
            release_dedupe_key(session, task)
        elif task.irreversible:
            task.status = TaskStatus.FAILED
            task.stage = "failed"
            task.progress = 1.0
            task.error = (
                "worker stopped after an irreversible step; the action may have "
                "completed — verify before retrying"
            )
        elif task.attempts >= task.max_attempts:
            task.status = TaskStatus.FAILED
            task.stage = "failed"
            task.progress = 1.0
            # Keep what the last attempt actually said. A worker that dies
            # after diagnosing the problem — or one whose finalizing write is
            # lost — otherwise leaves "lease expired", which explains nothing
            # and hides the sentence that would have told the user what to fix.
            task.error = (
                f"{task.error}\n(the worker then stopped responding on attempt "
                f"{task.attempts})"
                if task.error
                else f"lease expired after {task.attempts} attempt(s)"
            )
        else:
            task.status = TaskStatus.QUEUED
            task.stage = "retry_queued"
            task.error = "worker stopped responding; requeued"
        _clear_lease(task)
        task.updated_at = now
        session.add(task)
        changed.append(task)

    # A queued task the user cancelled while no worker was looking at it.
    pending_cancels = session.exec(
        select(Task)
        .where(Task.status == TaskStatus.QUEUED)
        .where(col(Task.cancel_requested) == True)  # noqa: E712
    ).all()
    for task in pending_cancels:
        task.status = TaskStatus.CANCELLED
        task.stage = "cancelled"
        task.progress = 1.0
        task.error = task.error or "cancelled by user"
        release_dedupe_key(session, task)
        _clear_lease(task)
        task.updated_at = now
        session.add(task)
        changed.append(task)

    if changed:
        session.commit()
        log.info("reclaimed %d task(s)", len(changed))
    return changed


def payload_of(task: Task) -> dict[str, Any]:
    return loads_dict(task.payload_json)


def _clear_lease(task: Task) -> None:
    task.heartbeat_at = None
    task.lease_expires_at = None
    task.updated_at = utc_now()
