"""Publications: the intent to post one video to one account at one time.

Two rules carry most of the weight here.

**A clip is scheduled at most once.** Enforced by the queue's unique
`dedupe_key`, so two racing requests cannot both create a publication for the
same clip — the previous version checked for duplicates by scanning task JSON,
which was both slow and racy.

**A failed publication is not automatically rescheduled.** It may have gone
live before failing. Re-queueing it needs an explicit retry from the user, who
can check the account first.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlmodel import Session, col, select

from app.core.clock import aware_utc_now, to_utc_naive, utc_now
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.jsonutil import dumps
from app.db.enums import ClipStatus, PublicationStatus, SourceKind, TaskKind, TaskStatus
from app.db.models import Account, Clip, ClipJob, Publication, Task
from app.domain import captions as caption_builder
from app.services import accounts as account_service
from app.tasks import queue

log = logging.getLogger(__name__)

# Long enough to notice a bulk retry and stop it, short enough not to be a wait.
RETRY_HEAD_START_MINUTES = 5
# Floor for the gap between two retried posts, whatever the original schedule
# said — publications that were all due at the same minute must not go out in
# the same minute either.
MIN_RETRY_SPACING_MINUTES = 2


def get(session: Session, publication_id: int) -> Publication:
    row = session.get(Publication, publication_id)
    if row is None:
        raise NotFoundError(f"publication {publication_id} not found")
    return row


def list_all(
    session: Session,
    *,
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[Publication]:
    query = select(Publication).order_by(col(Publication.scheduled_at))
    if status:
        query = query.where(Publication.status == status)
    return list(session.exec(query.offset(offset).limit(limit)).all())


def schedule_clip(
    session: Session,
    *,
    clip_id: int,
    username: str,
    scheduled_at: datetime,
    options: dict | None = None,
    caption: str | None = None,
) -> Publication:
    """Queue one rendered clip for publishing."""
    clip = session.get(Clip, clip_id)
    if clip is None:
        raise NotFoundError(f"clip {clip_id} not found")
    if clip.status != ClipStatus.READY:
        raise ConflictError("clip must finish rendering before it can be scheduled")
    if not clip.video_path:
        raise ConflictError("clip has no rendered file")

    # The request's own mistakes first. Checking the clip's existing booking
    # before them answered "this clip is already scheduled" to a request that
    # named an account which does not exist, or a time in the past — true, but
    # not the thing the caller got wrong.
    account = account_service.get_ready(session, username)
    _assert_future(scheduled_at)
    _assert_publishable(options)

    existing = active_for_clip(session, clip_id)
    if existing is not None:
        raise ConflictError(
            f"clip is already scheduled as publication #{existing.id} ({existing.status})"
        )

    job = session.get(ClipJob, clip.job_id)
    return _create(
        session,
        account=account,
        clip=clip,
        source_kind=SourceKind.CLIP,
        source_ref=clip.video_path,
        caption=caption or _caption_for(clip, job),
        scheduled_at=scheduled_at,
        options=options,
    )


def schedule_job(
    session: Session,
    *,
    job_id: int,
    username: str,
    first_at: datetime,
    interval_minutes: int = 60,
    options: dict | None = None,
) -> list[Publication]:
    """Queue every unscheduled clip of a job, spaced `interval_minutes` apart.

    Spacing matters: TikTok rate-limits bursts from one account, and a wall of
    clips posted at once reads as spam to viewers too.
    """
    from app.services import clips as clip_service

    account = account_service.get_ready(session, username)
    job = session.get(ClipJob, job_id)
    if job is None:
        raise NotFoundError(f"clip job {job_id} not found")

    pending = clip_service.unpublished(session, job_id)
    if not pending:
        raise ConflictError("this job has no rendered, unscheduled clips")

    created: list[Publication] = []
    for position, clip in enumerate(pending):
        created.append(
            _create(
                session,
                account=account,
                clip=clip,
                source_kind=SourceKind.CLIP,
                source_ref=clip.video_path or "",
                caption=_caption_for(clip, job),
                scheduled_at=first_at + timedelta(minutes=interval_minutes * position),
                options=options,
            )
        )
    log.info("scheduled %d clip(s) of job %s for '%s'", len(created), job_id, username)
    return created


def schedule_file(
    session: Session,
    *,
    username: str,
    source_ref: str,
    caption: str,
    scheduled_at: datetime,
    source_kind: SourceKind = SourceKind.LOCAL,
    options: dict | None = None,
) -> Publication:
    """Queue something that is not one of our clips — a local file or a URL."""
    account = account_service.get_ready(session, username)
    return _create(
        session,
        account=account,
        clip=None,
        source_kind=source_kind,
        source_ref=source_ref,
        caption=caption,
        scheduled_at=scheduled_at,
        options=options,
    )


def reschedule(session: Session, publication_id: int, scheduled_at: datetime) -> Publication:
    row = _editable(session, publication_id)
    _assert_future(scheduled_at)
    row.scheduled_at = to_utc_naive(scheduled_at)
    row.updated_at = utc_now()
    session.add(row)

    if row.task_id:
        task = session.get(Task, row.task_id)
        if task is not None and task.status == TaskStatus.QUEUED:
            task.run_at = row.scheduled_at
            task.updated_at = utc_now()
            session.add(task)
    session.flush()
    return row


def set_caption(session: Session, publication_id: int, caption: str) -> Publication:
    row = _editable(session, publication_id)
    row.caption = caption[:2200]
    row.updated_at = utc_now()
    session.add(row)
    session.flush()
    return row


def cancel(session: Session, publication_id: int) -> Publication:
    row = get(session, publication_id)
    if PublicationStatus(row.status).is_terminal:
        return row
    if row.status == PublicationStatus.PUBLISHING:
        raise ConflictError("this publication is being uploaded right now")

    if row.task_id:
        queue.request_cancel(session, row.task_id)
    row.status = PublicationStatus.CANCELLED
    row.result_text = "cancelled by user"
    row.updated_at = utc_now()
    session.add(row)
    session.flush()
    return row


def cancel_scheduled(session: Session, *, job_id: int | None = None) -> list[Publication]:
    """Bulk-cancel everything still waiting, optionally for one job only."""
    query = select(Publication).where(Publication.status == PublicationStatus.SCHEDULED)
    if job_id is not None:
        clip_ids = session.exec(select(col(Clip.id)).where(Clip.job_id == job_id)).all()
        query = query.where(col(Publication.clip_id).in_(clip_ids))

    cancelled = []
    for row in session.exec(query).all():
        cancelled.append(cancel(session, int(row.id)))
    return cancelled


def retry(
    session: Session, publication_id: int, *, at: datetime | None = None
) -> Publication:
    """Put a failed publication back in the queue.

    Deliberately manual. A publish can fail *after* TikTok accepted the video,
    so retrying automatically risks a duplicate post; a person should check the
    account first.

    `at` moves it to a specific time instead of straight away, which is what
    keeps a bulk retry from firing everything at once.
    """
    row = get(session, publication_id)
    if row.status != PublicationStatus.FAILED:
        raise ConflictError(f"only failed publications can be retried (this one is {row.status})")

    # Free the old key so the clip can be queued again.
    if row.task_id:
        old = session.get(Task, row.task_id)
        if old is not None:
            queue.release_dedupe_key(session, old)

    row.status = PublicationStatus.SCHEDULED
    row.result_text = None
    row.scheduled_at = to_utc_naive(at) if at is not None else utc_now()
    row.updated_at = utc_now()
    session.add(row)
    session.flush()
    _enqueue_task(session, row)
    return row


def retry_untouched(session: Session, *, job_id: int | None = None) -> list[Publication]:
    """Re-queue the failures where nothing was ever sent to TikTok.

    `retry` is deliberately one at a time, because an upload can fail *after*
    TikTok accepted the video and a blind retry would post it twice. That
    caution does not apply to a publication that never got as far as
    uploading: an account logged out, a missing file, a video that could not
    be resolved. The queue already records the difference — the handler marks
    its task irreversible on the line before the upload starts — so this
    retries exactly the ones that cannot duplicate anything.

    The case it exists for: one expired session turns every publication queued
    behind it into a separate failure, at its own scheduled minute, each
    needing its own click to recover. Twelve of them, in the run that led to
    this function.
    """
    query = select(Publication).where(Publication.status == PublicationStatus.FAILED)
    if job_id is not None:
        clip_ids = session.exec(select(col(Clip.id)).where(Clip.job_id == job_id)).all()
        query = query.where(col(Publication.clip_id).in_(clip_ids))

    candidates = [
        row
        for row in session.exec(query.order_by(col(Publication.scheduled_at))).all()
        if not _upload_may_have_started(session, row)
    ]

    retried: list[Publication] = []
    for slot, row in zip(_restart_times(candidates), candidates):
        retried.append(retry(session, int(row.id), at=slot))
    if retried:
        log.info("re-queued %d publication(s) that never reached TikTok", len(retried))
    return retried


def _restart_times(rows: list[Publication]) -> list[datetime]:
    """New times that keep the gaps the original schedule had.

    Sending all of them at once is the one thing a bulk retry must not do:
    TikTok rate-limits bursts from an account, and a wall of clips posted in
    one minute reads as spam to viewers as well. So the first goes out shortly
    from now and the rest keep the spacing they were given, whatever it was.
    """
    if not rows:
        return []
    start = aware_utc_now() + timedelta(minutes=RETRY_HEAD_START_MINUTES)
    times = [start]
    for previous, row in zip(rows, rows[1:]):
        gap = row.scheduled_at - previous.scheduled_at
        times.append(times[-1] + max(gap, timedelta(minutes=MIN_RETRY_SPACING_MINUTES)))
    return times


def _upload_may_have_started(session: Session, publication: Publication) -> bool:
    """Whether this publication's last attempt got as far as sending bytes.

    Unknown counts as yes. A task that has been swept away takes the evidence
    with it, and the safe reading of "no evidence" is that the video might be
    live.
    """
    if publication.task_id is None:
        return True
    task = session.get(Task, publication.task_id)
    return task is None or bool(task.irreversible)


def delete(session: Session, publication_id: int) -> None:
    row = get(session, publication_id)
    if row.status == PublicationStatus.PUBLISHING:
        raise ConflictError("cannot delete a publication that is being uploaded")
    if row.task_id:
        task = session.get(Task, row.task_id)
        if task is not None:
            queue.release_dedupe_key(session, task)
            if TaskStatus(task.status).is_active:
                queue.request_cancel(session, int(task.id))
    session.delete(row)
    session.flush()


def active_for_clip(session: Session, clip_id: int) -> Publication | None:
    """The publication that currently owns a clip, if any.

    Cancelled ones do not count — that is the whole point of cancelling.
    """
    blocking = [s for s in PublicationStatus if s.blocks_rescheduling]
    return session.exec(
        select(Publication)
        .where(Publication.clip_id == clip_id)
        .where(col(Publication.status).in_(blocking))
        .order_by(col(Publication.created_at).desc())
    ).first()


# ---- state transitions driven by the publish handler -----------------------


def mark_publishing(session: Session, publication_id: int) -> None:
    row = session.get(Publication, publication_id)
    if row is None:
        return
    row.status = PublicationStatus.PUBLISHING
    row.result_text = "uploading to TikTok"
    row.updated_at = utc_now()
    session.add(row)


def mark_published(session: Session, publication_id: int, *, video_id: str | None = None) -> None:
    row = session.get(Publication, publication_id)
    if row is None:
        return
    row.status = PublicationStatus.PUBLISHED
    row.result_text = f"published as {video_id}" if video_id else "published"
    row.published_at = utc_now()
    row.updated_at = utc_now()
    session.add(row)

    account = session.get(Account, row.account_id)
    if account is not None:
        account.last_used_at = utc_now()
        session.add(account)


def mark_failed(session: Session, publication_id: int, error: str) -> None:
    row = session.get(Publication, publication_id)
    if row is None:
        return
    row.status = PublicationStatus.FAILED
    row.result_text = error[:2048]
    row.updated_at = utc_now()
    session.add(row)


# ---- internals -------------------------------------------------------------


def _create(
    session: Session,
    *,
    account: Account,
    clip: Clip | None,
    source_kind: SourceKind,
    source_ref: str,
    caption: str,
    scheduled_at: datetime,
    options: dict | None,
) -> Publication:
    _assert_future(scheduled_at)
    _assert_publishable(options)
    options = dict(options or {})

    row = Publication(
        account_id=account.id,
        clip_id=clip.id if clip else None,
        source_kind=str(source_kind),
        source_ref=source_ref,
        caption=caption[:2200],
        options_json=dumps(options),
        scheduled_at=to_utc_naive(scheduled_at),
        status=PublicationStatus.SCHEDULED,
    )
    session.add(row)
    session.flush()
    _enqueue_task(session, row)
    return row


def _enqueue_task(session: Session, publication: Publication) -> Task:
    task = queue.enqueue(
        session,
        TaskKind.PUBLISH,
        {"publication_id": publication.id},
        run_at=publication.scheduled_at,
        dedupe_key=f"publish:publication:{publication.id}",
    )
    publication.task_id = task.id
    session.add(publication)
    session.flush()
    return task


def _editable(session: Session, publication_id: int) -> Publication:
    row = get(session, publication_id)
    if row.status != PublicationStatus.SCHEDULED:
        raise ConflictError(f"cannot modify a publication in state '{row.status}'")
    return row


def _assert_publishable(options: dict | None) -> None:
    """Reject options TikTok will refuse, before anything is written down.

    A private video cannot be scheduled: TikTok will not accept it, and
    finding that out at publish time wastes the slot. Checked at the top of
    `schedule_clip` as well as here, because a request that gets both this and
    the clip's existing booking wrong should hear about its own mistake first.
    """
    if options and int(options.get("visibility_type", 0)) == 1:
        raise ValidationError("private videos (visibility_type=1) cannot be scheduled")


def _assert_future(value: datetime) -> None:
    if value.tzinfo is None:
        raise ValidationError("scheduled_at must include timezone information")
    if value <= aware_utc_now():
        raise ValidationError("scheduled_at must be in the future")


def _caption_for(clip: Clip, job: ClipJob | None) -> str:
    return caption_builder.caption_for_clip(
        source_title=job.display_title if job else None,
        clip_title=clip.title,
        clip_text=clip.text,
        index=clip.index,
        caption_tags=job.caption_tags if job else None,
    )
