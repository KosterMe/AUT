"""Clips: the individual videos cut out of a job's source.

A clip is a row here, not a JSON fragment inside a task result. That is what
makes "list a job's clips", "which clips are unscheduled" and "delete expired
files" ordinary queries instead of table scans with JSON parsing.
"""
from __future__ import annotations

import json
import logging
import os

from sqlmodel import Session, col, select

from app.core.clock import utc_now
from app.core.errors import ConflictError, NotFoundError
from app.db.enums import ClipStatus, TaskKind
from app.db.models import Clip, ClipJob, Publication
from app.db.enums import PublicationStatus
from app.domain.cutting import SliceSpec
from app.services import clip_jobs
from app.tasks import queue

log = logging.getLogger(__name__)

# A clip is "spoken for" in any of these states. Cancelled is absent on
# purpose: cancelling a publication is what frees its clip to be scheduled again.
_BLOCKING_STATUSES = [status for status in PublicationStatus if status.blocks_rescheduling]


def get(session: Session, clip_id: int) -> Clip:
    clip = session.get(Clip, clip_id)
    if clip is None:
        raise NotFoundError(f"clip {clip_id} not found")
    return clip


def list_for_job(session: Session, job_id: int) -> list[Clip]:
    return list(
        session.exec(select(Clip).where(Clip.job_id == job_id).order_by(col(Clip.index))).all()
    )


def plan(session: Session, job_id: int, specs: list[SliceSpec]) -> list[Clip]:
    """Turn slice boundaries into clip rows and queue a render for each.

    Boundaries are all this takes. How the clips are dressed is on the job,
    where it was recorded when the job started, so cutting neither knows nor
    forwards it.

    Replanning a job replaces any clips that were never rendered; clips that
    already produced a file are left alone so a re-run does not throw away
    work — or orphan a file a publication points at.
    """
    _drop_unrendered(session, job_id)

    clips: list[Clip] = []
    for spec in specs:
        clip = Clip(
            job_id=job_id,
            index=spec.index,
            start_sec=spec.start_sec,
            end_sec=spec.end_sec,
            duration_sec=spec.duration_sec,
            title=spec.title[:512],
            text=spec.text[:4096],
            status=ClipStatus.PLANNED,
        )
        session.add(clip)
        clips.append(clip)
    session.flush()

    for clip in clips:
        task = queue.enqueue(
            session,
            TaskKind.RENDER,
            {"clip_id": clip.id, "job_id": job_id},
            dedupe_key=f"render:clip:{clip.id}",
        )
        clip.task_id = task.id
        session.add(clip)
    session.flush()

    log.info("planned %d clip(s) for job %s", len(clips), job_id)
    return clips


def mark_rendering(session: Session, clip_id: int) -> None:
    clip = session.get(Clip, clip_id)
    if clip is None:
        return
    clip.status = ClipStatus.RENDERING
    clip.error = None
    clip.updated_at = utc_now()
    session.add(clip)
    clip_jobs.refresh_status(session, clip.job_id)


def mark_ready(
    session: Session,
    clip_id: int,
    *,
    video_path: str,
    cover_path: str | None,
    composition: dict | None = None,
) -> Clip:
    """Record a finished clip, and what it was made of.

    The composition is stored with it rather than thrown away: it is the only
    record of which pieces of the source play in what order, what was laid over
    them and which style did it — so a finished clip can be inspected, and
    re-rendered without re-deciding anything.
    """
    clip = get(session, clip_id)
    clip.status = ClipStatus.READY
    clip.video_path = video_path
    clip.cover_path = cover_path
    clip.error = None
    if composition is not None:
        clip.composition_json = json.dumps(composition, ensure_ascii=False)
    clip.updated_at = utc_now()
    session.add(clip)
    clip_jobs.refresh_status(session, clip.job_id)
    return clip


def composition_of(session: Session, clip_id: int) -> dict:
    """What a clip was last rendered from, or an empty dict if it never was."""
    clip = get(session, clip_id)
    try:
        data = json.loads(clip.composition_json or "{}")
    except json.JSONDecodeError:
        log.warning("clip %s holds an unreadable composition", clip_id)
        return {}
    return data if isinstance(data, dict) else {}


def rerender(session: Session, clip_id: int, *, style: dict | None = None) -> Clip:
    """Queue this one clip again, optionally with a different look.

    One clip, not the job: replanning a job recuts every clip in it, which is
    the wrong tool for "I want this one to look different". The boundaries stay
    exactly where they are and only the render is repeated — cheap, because the
    transcript is already cached and the segments may still be in the fragment
    cache.
    """
    clip = get(session, clip_id)
    if clip.status == ClipStatus.RENDERING:
        raise ConflictError(f"clip {clip_id} is already rendering")

    options = dict(render_options_of(session, clip))
    if style is not None:
        options["style"] = style

    clip.status = ClipStatus.PLANNED
    clip.error = None
    clip.updated_at = utc_now()
    task = queue.enqueue(
        session,
        TaskKind.RENDER,
        {"clip_id": clip.id, "job_id": clip.job_id, "render": options},
        # Deliberately not the plan-time key: that one would be deduplicated
        # against the render this clip already had, and nothing would happen.
        dedupe_key=f"render:clip:{clip.id}:{int(utc_now().timestamp())}",
    )
    clip.task_id = task.id
    session.add(clip)
    clip_jobs.refresh_status(session, clip.job_id)
    session.flush()
    log.info("queued a re-render of clip %s as task %s", clip_id, task.id)
    return clip


def render_options_of(session: Session, clip: Clip) -> dict:
    """The montage options this clip renders with.

    The job's, because that is where they live. The fallback reads them back
    off the clip's own render task, which is where they lived before: clips
    planned by an older worker have them there and nowhere else, and a
    re-render that found nothing would go looking for a source with no path.
    """
    job = session.get(ClipJob, clip.job_id)
    stored = clip_jobs.montage_options(job) if job is not None else {}
    return stored or _queued_options(session, clip)


def _queued_options(session: Session, clip: Clip) -> dict:
    """Montage options off a render task queued before the job kept its own."""
    from app.db.models import Task

    task = session.get(Task, clip.task_id) if clip.task_id else None
    if task is None:
        return {}
    payload = queue.payload_of(task)
    options = payload.get("render") if isinstance(payload, dict) else None
    return dict(options) if isinstance(options, dict) else {}


def mark_failed(session: Session, clip_id: int, error: str) -> None:
    clip = session.get(Clip, clip_id)
    if clip is None:
        return
    clip.status = ClipStatus.FAILED
    clip.error = error[:2048]
    clip.updated_at = utc_now()
    session.add(clip)
    clip_jobs.refresh_status(session, clip.job_id)


def mark_cancelled(session: Session, clip_id: int) -> None:
    clip = session.get(Clip, clip_id)
    if clip is None:
        return
    clip.status = ClipStatus.CANCELLED
    clip.error = "cancelled by user"
    clip.updated_at = utc_now()
    session.add(clip)
    clip_jobs.refresh_status(session, clip.job_id)


def publications_by_clip(session: Session, job_id: int) -> dict[int, Publication]:
    """The active publication for each of a job's clips, keyed by clip id.

    One query for the whole job, so a clip list can show what is already
    scheduled without asking per clip.
    """
    rows = session.exec(
        select(Publication)
        .join(Clip, col(Clip.id) == col(Publication.clip_id))
        .where(Clip.job_id == job_id)
        .where(col(Publication.status).in_(_BLOCKING_STATUSES))
        .order_by(col(Publication.created_at))
    ).all()
    # Later rows win, so the most recent publication is the one reported.
    return {int(row.clip_id): row for row in rows if row.clip_id is not None}


def unpublished(session: Session, job_id: int) -> list[Clip]:
    """Ready clips with no active publication.

    One indexed join, where the previous version loaded every render task in
    the database and JSON-parsed each one to answer the same question.
    """
    spoken_for = select(col(Publication.clip_id)).where(
        col(Publication.status).in_(_BLOCKING_STATUSES)
    )
    return list(
        session.exec(
            select(Clip)
            .where(Clip.job_id == job_id)
            .where(Clip.status == ClipStatus.READY)
            .where(col(Clip.id).notin_(spoken_for))
            .order_by(col(Clip.index))
        ).all()
    )


def delete_files(clip: Clip) -> int:
    """Remove a clip's rendered artefacts from disk. Returns how many went."""
    removed = 0
    for path in (clip.video_path, clip.cover_path):
        if path and os.path.exists(path):
            try:
                os.remove(path)
                removed += 1
            except OSError as exc:  # pragma: no cover - filesystem edge case
                log.warning("could not delete %s: %s", path, exc)
    return removed


def _drop_unrendered(session: Session, job_id: int) -> None:
    stale = session.exec(
        select(Clip)
        .where(Clip.job_id == job_id)
        .where(col(Clip.status).in_([ClipStatus.PLANNED, ClipStatus.FAILED, ClipStatus.CANCELLED]))
    ).all()
    for clip in stale:
        session.delete(clip)
    if stale:
        session.flush()
        log.debug("dropped %d unrendered clip(s) from job %s before replanning", len(stale), job_id)
