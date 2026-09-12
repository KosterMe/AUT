"""Queue semantics: claiming, leases, retries, cancellation, dedupe.

These are the rules the whole system leans on, so they are tested directly
rather than through a handler.
"""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlmodel import select

from app.core.clock import utc_now
from app.core.errors import PermanentError, TaskCancelled
from app.db.enums import TaskKind, TaskStatus
from app.db.models import Task
from app.db.session import session_scope
from app.tasks import queue, runner
from app.tasks.registry import _HANDLERS


@pytest.fixture(autouse=True)
def _schema(db):
    """Every test in this module needs the tables."""
    yield


@pytest.fixture(autouse=True)
def _clean_handlers():
    """An empty registry per test.

    These tests register their own trivial handlers, so the real ones (which
    another test module may already have imported) must be out of the way, and
    must be put back afterwards.
    """
    saved = dict(_HANDLERS)
    _HANDLERS.clear()
    yield
    _HANDLERS.clear()
    _HANDLERS.update(saved)


def _enqueue(**kwargs) -> int:
    with session_scope() as session:
        task = queue.enqueue(session, kwargs.pop("kind", TaskKind.RENDER), **kwargs)
        session.flush()
        return int(task.id)


def _get(task_id: int) -> Task:
    with session_scope() as session:
        task = session.get(Task, task_id)
        session.expunge(task)
        return task


def test_claim_marks_running_and_counts_the_attempt():
    task_id = _enqueue()

    with session_scope() as session:
        claimed = queue.claim(session, [TaskKind.RENDER])

    assert claimed is not None and claimed.id == task_id
    stored = _get(task_id)
    assert stored.status == TaskStatus.RUNNING
    assert stored.attempts == 1
    assert stored.lease_expires_at is not None


def test_claim_respects_kind_filter():
    _enqueue(kind=TaskKind.PUBLISH)

    with session_scope() as session:
        assert queue.claim(session, [TaskKind.RENDER]) is None
        assert queue.claim(session, [TaskKind.PUBLISH]) is not None


def test_claim_skips_tasks_scheduled_for_later():
    _enqueue(run_at=utc_now() + timedelta(hours=1))

    with session_scope() as session:
        assert queue.claim(session, [TaskKind.RENDER]) is None


def test_claim_prefers_higher_priority():
    _enqueue(priority=0, payload={"tag": "low"})
    high = _enqueue(priority=10, payload={"tag": "high"})

    with session_scope() as session:
        claimed = queue.claim(session, [TaskKind.RENDER])

    assert claimed is not None and claimed.id == high


def test_only_one_of_two_racing_workers_claims_a_task():
    _enqueue()
    claimed: list[int] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        with session_scope() as session:
            task = queue.claim(session, [TaskKind.RENDER])
            if task is not None:
                claimed.append(int(task.id))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(claimed) == 1


def test_dedupe_key_returns_the_existing_task_instead_of_a_duplicate():
    first = _enqueue(dedupe_key="publish:clip:7")
    second = _enqueue(dedupe_key="publish:clip:7")

    assert first == second
    with session_scope() as session:
        assert len(session.exec(select(Task)).all()) == 1


def test_a_finished_task_does_not_block_an_equivalent_new_one():
    """Deduplication is about work still coming, not work already done.

    Without this a failed download could never be restarted: the caller would
    silently get the old failed task back and nothing would ever run.
    """
    first = _enqueue(dedupe_key="download:job:3")
    with session_scope() as session:
        queue.claim(session, [TaskKind.RENDER])
        queue.finish_failure(session, first, "YouTube said no", permanent=True)

    second = _enqueue(dedupe_key="download:job:3")

    assert second != first
    assert _get(second).status == TaskStatus.QUEUED
    # The old attempt is kept as history, just without the key.
    assert _get(first).status == TaskStatus.FAILED
    assert _get(first).dedupe_key is None


def test_an_active_task_still_deduplicates():
    first = _enqueue(dedupe_key="download:job:4")
    with session_scope() as session:
        queue.claim(session, [TaskKind.RENDER])

    # Running, so a second request must join it rather than start a rival.
    assert _enqueue(dedupe_key="download:job:4") == first


def test_releasing_a_dedupe_key_allows_requeueing():
    first = _enqueue(dedupe_key="publish:clip:7")
    with session_scope() as session:
        queue.release_dedupe_key(session, session.get(Task, first))

    second = _enqueue(dedupe_key="publish:clip:7")
    assert second != first


def test_transient_failure_requeues_with_backoff():
    task_id = _enqueue(max_attempts=3)
    with session_scope() as session:
        queue.claim(session, [TaskKind.RENDER])
        queue.finish_failure(session, task_id, "network blip")

    stored = _get(task_id)
    assert stored.status == TaskStatus.QUEUED
    assert stored.error == "network blip"
    # Backed off rather than retried instantly, so a flapping dependency does
    # not consume every attempt in a couple of seconds.
    assert stored.run_at > utc_now()


def test_permanent_failure_skips_remaining_attempts():
    task_id = _enqueue(max_attempts=3)
    with session_scope() as session:
        queue.claim(session, [TaskKind.RENDER])
        queue.finish_failure(session, task_id, "account has no session", permanent=True)

    stored = _get(task_id)
    assert stored.status == TaskStatus.FAILED
    assert stored.attempts == 1


def test_failure_after_last_attempt_is_final():
    task_id = _enqueue(max_attempts=1)
    with session_scope() as session:
        queue.claim(session, [TaskKind.RENDER])
        queue.finish_failure(session, task_id, "still broken")

    assert _get(task_id).status == TaskStatus.FAILED


def test_expired_lease_requeues_the_task():
    task_id = _enqueue(max_attempts=3)
    with session_scope() as session:
        queue.claim(session, [TaskKind.RENDER])
        # Simulate a worker that died: lease in the past, no heartbeat since.
        task = session.get(Task, task_id)
        task.lease_expires_at = utc_now() - timedelta(seconds=1)
        session.add(task)

    with session_scope() as session:
        reclaimed = queue.reclaim_expired(session)

    assert [t.id for t in reclaimed] == [task_id]
    assert _get(task_id).status == TaskStatus.QUEUED


def test_a_stale_sweep_cannot_resurrect_a_task_another_worker_claimed(monkeypatch):
    """Two sweeps read the same orphan; the loser must not hand it out again.

    Every worker thread sweeps for expired leases, so several of them read the
    same row in the same instant. A sweeper that writes its decision without
    re-checking the row requeues a task another worker has already claimed,
    and then claims it itself — the handler runs twice, in parallel. This
    happened: one file was transcribed by two Whisper passes a second apart.
    """
    task_id = _enqueue(max_attempts=3)
    with session_scope() as session:
        queue.claim(session, [TaskKind.RENDER])
        task = session.get(Task, task_id)
        task.lease_expires_at = utc_now() - timedelta(seconds=1)
        session.add(task)

    original = queue._orphan_outcome

    def other_worker_gets_there_first(task):
        # Called after this sweep has read the orphan and before it writes:
        # exactly the window the second worker slips through.
        monkeypatch.setattr(queue, "_orphan_outcome", original)
        with session_scope() as other:
            queue.reclaim_expired(other)
            assert queue.claim(other, [TaskKind.RENDER]) is not None
        return original(task)

    monkeypatch.setattr(queue, "_orphan_outcome", other_worker_gets_there_first)

    with session_scope() as session:
        reclaimed = queue.reclaim_expired(session)

    assert reclaimed == []
    assert _get(task_id).status == TaskStatus.RUNNING


def test_reclaiming_a_last_attempt_keeps_what_went_wrong():
    """The diagnosis outlives the worker that produced it.

    A worker can name the problem and then lose the write that records the
    failure — its final attempt is swept up by the lease instead. Replacing the
    message with "lease expired" throws away the only sentence that says what
    the user has to fix.
    """
    task_id = _enqueue(max_attempts=1)
    with session_scope() as session:
        queue.claim(session, [TaskKind.RENDER])
        task = session.get(Task, task_id)
        task.error = "YouTube could not be reached at all"
        task.lease_expires_at = utc_now() - timedelta(seconds=1)
        session.add(task)

    with session_scope() as session:
        queue.reclaim_expired(session)

    stored = _get(task_id)
    assert stored.status == TaskStatus.FAILED
    assert "YouTube could not be reached at all" in stored.error
    assert "stopped responding" in stored.error


def test_reclaiming_a_silent_last_attempt_still_says_why():
    task_id = _enqueue(max_attempts=1)
    with session_scope() as session:
        queue.claim(session, [TaskKind.RENDER])
        task = session.get(Task, task_id)
        task.lease_expires_at = utc_now() - timedelta(seconds=1)
        session.add(task)

    with session_scope() as session:
        queue.reclaim_expired(session)

    assert "lease expired after 1 attempt(s)" in _get(task_id).error


def test_reclaiming_a_cancelled_task_says_it_was_cancelled():
    """The task ends as cancelled, not as whatever went wrong last time.

    Cancelling a running task records no reason of its own, so the error
    column still holds the previous attempt's. Carrying it over made a task
    the user had just cancelled report "worker stopped responding; requeued".
    """
    task_id = _enqueue(max_attempts=3)
    with session_scope() as session:
        queue.claim(session, [TaskKind.RENDER])
        queue.request_cancel(session, task_id)
        task = session.get(Task, task_id)
        # What an earlier attempt left in the column. Cancelling a running
        # task writes no reason of its own, so this is what survives.
        task.error = "RuntimeError: something from last time"
        task.lease_expires_at = utc_now() - timedelta(seconds=1)
        session.add(task)

    with session_scope() as session:
        reclaimed = queue.reclaim_expired(session)

    assert [t.status for t in reclaimed] == [TaskStatus.CANCELLED]
    assert _get(task_id).error == "cancelled by user"


def test_expired_lease_after_an_irreversible_step_fails_instead_of_retrying():
    """The duplicate-post guard: a publish that died mid-upload must not be
    retried automatically, because the video may already be live."""
    task_id = _enqueue(kind=TaskKind.PUBLISH, max_attempts=3)
    with session_scope() as session:
        queue.claim(session, [TaskKind.PUBLISH])
        queue.mark_irreversible(session, task_id)
        task = session.get(Task, task_id)
        task.lease_expires_at = utc_now() - timedelta(seconds=1)
        session.add(task)

    with session_scope() as session:
        queue.reclaim_expired(session)

    stored = _get(task_id)
    assert stored.status == TaskStatus.FAILED
    assert "verify before retrying" in stored.error


def test_cancelling_a_queued_task_takes_effect_immediately():
    task_id = _enqueue()
    with session_scope() as session:
        queue.request_cancel(session, task_id)

    stored = _get(task_id)
    assert stored.status == TaskStatus.CANCELLED
    # The key is freed, so the same work can be queued again later.
    assert stored.dedupe_key is None

    with session_scope() as session:
        assert queue.claim(session, [TaskKind.RENDER]) is None


def test_cancelling_a_running_task_only_requests_it():
    task_id = _enqueue()
    with session_scope() as session:
        queue.claim(session, [TaskKind.RENDER])
        queue.request_cancel(session, task_id)

    stored = _get(task_id)
    assert stored.status == TaskStatus.RUNNING
    assert stored.cancel_requested is True


def test_runner_records_handler_result():
    from app.tasks.registry import register_handler

    @register_handler(TaskKind.CLEANUP)
    def _handler(ctx):
        ctx.progress("working", 0.5)
        return {"deleted": 3}

    task_id = _enqueue(kind=TaskKind.CLEANUP)
    assert runner.run_once([TaskKind.CLEANUP]) is True
    assert runner.run_once([TaskKind.CLEANUP]) is False

    stored = _get(task_id)
    assert stored.status == TaskStatus.SUCCEEDED
    assert '"deleted": 3' in stored.result_json


def test_runner_maps_permanent_error_to_a_final_failure():
    from app.tasks.registry import register_handler

    @register_handler(TaskKind.CLEANUP)
    def _handler(ctx):
        raise PermanentError("nothing to work with")

    task_id = _enqueue(kind=TaskKind.CLEANUP, max_attempts=3)
    runner.run_once([TaskKind.CLEANUP])

    stored = _get(task_id)
    assert stored.status == TaskStatus.FAILED
    assert stored.attempts == 1


def test_handler_observes_cancellation_through_progress():
    from app.tasks.registry import register_handler

    @register_handler(TaskKind.CLEANUP)
    def _handler(ctx):
        with session_scope() as session:
            queue.request_cancel(session, ctx.task_id)
        ctx.progress("second step", 0.6)  # must raise
        raise AssertionError("handler continued past cancellation")

    task_id = _enqueue(kind=TaskKind.CLEANUP)
    runner.run_once([TaskKind.CLEANUP])

    assert _get(task_id).status == TaskStatus.CANCELLED


def test_missing_handler_fails_the_task_permanently():
    task_id = _enqueue(kind=TaskKind.CLEANUP)
    runner.run_once([TaskKind.CLEANUP])

    stored = _get(task_id)
    assert stored.status == TaskStatus.FAILED
    assert "no handler registered" in stored.error


def test_a_worker_that_loses_its_lease_stops_and_writes_nothing(monkeypatch, configure):
    """Two workers must never be running one task at the same time.

    The lease refresher noticed the loss and quietly returned, leaving the
    handler to carry on with a task somebody else had already claimed — two
    renders writing one output file, or in the worst case two uploads of the
    same video. It now stops the handler, and the abandoned worker reports
    nothing: the task's state belongs to whoever holds it now.
    """
    import time

    from app.tasks.registry import register_handler

    configure(QUEUE_HEARTBEAT_SECONDS=0.05)
    steps: list[str] = []

    @register_handler(TaskKind.CLEANUP)
    def _handler(ctx):
        steps.append("started")
        time.sleep(0.4)          # long enough for the refresher to try once
        ctx.progress("working", 0.5)
        steps.append("kept going")
        return {"deleted": 3}

    task_id = _enqueue(kind=TaskKind.CLEANUP)
    # The task is no longer ours: this is what an expired lease looks like
    # from the refresher's side once another worker has reclaimed it.
    monkeypatch.setattr(queue, "heartbeat", lambda session, task_id: False)

    runner.run_once([TaskKind.CLEANUP])

    stored = _get(task_id)
    assert steps == ["started"], "the handler must not run on past the loss"
    assert stored.status == TaskStatus.RUNNING, "the abandoned worker must not finalize it"
    assert "deleted" not in (stored.result_json or ""), "nor record a result for it"


def test_task_cancelled_exception_is_not_a_failure():
    from app.tasks.registry import register_handler

    @register_handler(TaskKind.CLEANUP)
    def _handler(ctx):
        raise TaskCancelled("stopped")

    task_id = _enqueue(kind=TaskKind.CLEANUP)
    runner.run_once([TaskKind.CLEANUP])

    assert _get(task_id).status == TaskStatus.CANCELLED
