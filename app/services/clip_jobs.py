"""Clip jobs: a source video and the clips cut out of it.

The one rule worth stating up front: **`refresh_status` is the only function
that decides a job's status**, and it derives it from the job's clips. The
previous version recomputed job status in three separate places by scanning
sibling task rows and rewriting a JSON blob, which is how a job could end up
"failed" with every clip rendered and "sliced" with none.
"""
from __future__ import annotations

import logging
import os

from sqlmodel import Session, col, select

from app.core.clock import utc_now
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.jsonutil import dumps
from app.db.enums import ClipStatus, JobStatus, TaskKind, TaskStatus
from app.db.models import Clip, ClipJob, Task
from app.domain import profiles, sources
from app.tasks import queue

log = logging.getLogger(__name__)


def create(
    session: Session,
    *,
    source_ref: str,
    source_platform: str = "auto",
    title: str | None = None,
    custom_title: str | None = None,
    caption_tags: str | None = None,
    profile: str | None = None,
    start_immediately: bool = False,
    min_clip_seconds: float | None = None,
    max_clip_seconds: float | None = None,
    gap_seconds: float | None = None,
    max_clips: int = 0,
    render: dict | None = None,
) -> ClipJob:
    """Register a source video, optionally starting work on it right away."""
    source_ref = source_ref.strip()
    if not source_ref:
        raise ValidationError("source_ref is required")

    platform = (
        sources.infer_platform(source_ref) if source_platform == "auto" else source_platform
    )
    if platform == sources.PLATFORM_LOCAL and not os.path.exists(source_ref):
        raise ValidationError(f"local source file does not exist: {source_ref}")

    try:
        chosen = profiles.get(profile)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    job = ClipJob(
        source_platform=platform,
        source_ref=source_ref,
        title=title,
        custom_title=_normalize_title(custom_title),
        caption_tags=caption_tags.strip() if caption_tags else None,
        profile=chosen.name,
        status=JobStatus.CREATED,
    )
    session.add(job)
    session.flush()

    if start_immediately:
        start(
            session,
            job.id,
            min_clip_seconds=min_clip_seconds,
            max_clip_seconds=max_clip_seconds,
            gap_seconds=gap_seconds,
            max_clips=max_clips,
            render=render,
        )
    return job


def get(session: Session, job_id: int) -> ClipJob:
    job = session.get(ClipJob, job_id)
    if job is None:
        raise NotFoundError(f"clip job {job_id} not found")
    return job


def list_all(session: Session, *, limit: int = 100, offset: int = 0) -> list[ClipJob]:
    return list(
        session.exec(
            select(ClipJob)
            .order_by(col(ClipJob.created_at).desc())
            .offset(offset)
            .limit(limit)
        ).all()
    )


def start(
    session: Session,
    job_id: int,
    *,
    min_clip_seconds: float | None = None,
    max_clip_seconds: float | None = None,
    gap_seconds: float | None = None,
    max_clips: int = 0,
    profile: str | None = None,
    render: dict | None = None,
) -> Task:
    """Queue the download → transcribe → plan pass for a job.

    Idempotent: a job that already has work in flight returns that task rather
    than queueing a second one, so a double-clicked button cannot download the
    same video twice.
    """
    job = get(session, job_id)

    existing = _active_task_for_job(session, job_id)
    if existing is not None:
        return existing

    if profile is not None:
        try:
            job.profile = profiles.get(profile).name
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    # Resolved once, here, so the download and every render of this job agree
    # about what kind of video it is — including renders queued days later.
    settings = profiles.resolve(profiles.get(job.profile), render or {})

    payload = {
        "job_id": job_id,
        "profile": settings["profile"],
        "cutter": settings["cutter"],
        "min_clip_seconds": min_clip_seconds,
        "max_clip_seconds": max_clip_seconds,
        "gap_seconds": gap_seconds,
        "max_clips": max_clips,
        "render": {**(render or {}), **settings},
    }
    task = queue.enqueue(
        session,
        TaskKind.DOWNLOAD,
        payload,
        dedupe_key=f"download:job:{job_id}",
    )
    job.status = JobStatus.DOWNLOADING
    job.stage = "queued"
    job.progress = 0.0
    job.error = None
    job.updated_at = utc_now()
    session.add(job)
    session.flush()
    log.info("queued download task %s for job %s", task.id, job_id)
    return task


def set_custom_title(session: Session, job_id: int, custom_title: str) -> ClipJob:
    """Override the auto-detected name. Empty clears it.

    Applied to the job itself immediately so lists and caption previews show
    it before anything is rendered.
    """
    job = get(session, job_id)
    job.custom_title = _normalize_title(custom_title)
    job.updated_at = utc_now()
    session.add(job)
    session.flush()
    return job


def set_caption_tags(session: Session, job_id: int, caption_tags: str) -> ClipJob:
    """Set the tags used for every clip of this job. Empty falls back to the
    global defaults plus auto-detected topic tags."""
    job = get(session, job_id)
    normalized = " ".join(caption_tags.split())[:500]
    job.caption_tags = normalized or None
    job.updated_at = utc_now()
    session.add(job)
    session.flush()
    return job


def cancel(session: Session, job_id: int) -> ClipJob:
    """Stop everything in flight for a job.

    Queued tasks end immediately; running ones are asked to stop and their
    handlers notice at the next progress checkpoint.
    """
    job = get(session, job_id)
    tasks = session.exec(
        select(Task)
        .where(col(Task.status).in_([TaskStatus.QUEUED, TaskStatus.RUNNING]))
        .where(col(Task.dedupe_key).in_(_job_task_keys(session, job_id)))
    ).all()
    for task in tasks:
        queue.request_cancel(session, int(task.id))

    for clip in _clips(session, job_id):
        if not ClipStatus(clip.status).is_terminal:
            clip.status = ClipStatus.CANCELLED
            clip.error = "cancelled by user"
            clip.updated_at = utc_now()
            session.add(clip)

    job.status = JobStatus.CANCELLED
    job.error = "cancelled by user"
    job.updated_at = utc_now()
    session.add(job)
    session.flush()
    log.info("cancelled job %s (%d task(s))", job_id, len(tasks))
    return job


def delete(session: Session, job_id: int) -> None:
    """Remove a job and its clips. Refuses while work is in flight."""
    job = get(session, job_id)
    if _active_task_for_job(session, job_id) is not None:
        raise ConflictError("cancel the job before deleting it")
    session.delete(job)  # clips cascade
    session.flush()


def record_source(
    session: Session,
    job_id: int,
    *,
    original_path: str,
    duration_seconds: float | None,
    title: str | None,
    thumbnail_path: str | None,
    metadata: dict,
) -> ClipJob:
    """Save what was learned about the source after downloading it."""
    job = get(session, job_id)
    job.original_path = original_path
    job.duration_seconds = duration_seconds or job.duration_seconds
    # A manual title always wins: the user set it precisely to override this.
    # A detected one replaces whatever was there. And when none was detected —
    # a cached file whose metadata could not be refreshed — the existing title
    # is kept rather than replaced by the media filename, because the title is
    # the first line of every caption this job will publish.
    if title and not job.custom_title:
        job.title = title
    elif not job.title:
        job.title = os.path.basename(original_path)
    job.thumbnail_path = thumbnail_path
    job.source_metadata_json = dumps(_compact_metadata(metadata))
    job.updated_at = utc_now()
    session.add(job)
    session.flush()
    return job


def set_progress(session: Session, job_id: int, status: JobStatus, stage: str, progress: float) -> None:
    """Mirror an in-flight task's progress onto the job.

    Kept on the job so listing jobs never has to join the queue — the UI polls
    that list every few seconds.
    """
    job = session.get(ClipJob, job_id)
    if job is None:
        return
    job.status = status
    job.stage = stage
    job.progress = max(0.0, min(1.0, progress))
    # Reaching a new stage settles the last one: an error left over from a
    # previous attempt would otherwise be shown against work that is going fine.
    job.error = None
    job.updated_at = utc_now()
    session.add(job)


def set_retrying(session: Session, job_id: int, error: str, *, attempt: int) -> None:
    """Record a failed attempt that the queue is going to try again.

    Not `fail`: the job is still on its way. But the retry backoff is minutes
    long, and without this the job spends them insisting it is "queued" — which
    is indistinguishable from a worker that never picked it up at all.
    """
    job = session.get(ClipJob, job_id)
    if job is None:
        return
    job.stage = f"attempt {attempt} failed, retrying"
    job.error = error[:2048]
    job.updated_at = utc_now()
    session.add(job)


def fail(session: Session, job_id: int, error: str) -> None:
    job = session.get(ClipJob, job_id)
    if job is None:
        return
    job.status = JobStatus.FAILED
    job.error = error[:2048]
    job.stage = "failed"
    job.progress = 1.0
    job.updated_at = utc_now()
    session.add(job)


def refresh_status(session: Session, job_id: int) -> ClipJob | None:
    """Derive a job's status from its clips. The only place that decides it.

    Called after any clip changes state. A job is READY when every clip has
    finished and at least one produced a file; FAILED only when nothing did.
    """
    job = session.get(ClipJob, job_id)
    if job is None:
        return None

    clips = _clips(session, job_id)
    if not clips:
        return job

    ready = [c for c in clips if c.status == ClipStatus.READY]
    failed = [c for c in clips if c.status == ClipStatus.FAILED]
    cancelled = [c for c in clips if c.status == ClipStatus.CANCELLED]
    pending = [c for c in clips if not ClipStatus(c.status).is_terminal]

    if pending:
        job.status = JobStatus.RENDERING
        job.stage = f"rendering {len(ready)}/{len(clips)}"
        job.progress = len(ready) / len(clips)
        job.error = None
    elif ready:
        # Some clips failing does not make the job a failure — the rest are
        # publishable, which is the whole point of cutting many clips.
        job.status = JobStatus.READY
        job.stage = "ready"
        job.progress = 1.0
        job.error = (
            f"{len(failed)} of {len(clips)} clips failed to render" if failed else None
        )
    elif cancelled and not failed:
        job.status = JobStatus.CANCELLED
        job.stage = "cancelled"
        job.progress = 1.0
    else:
        job.status = JobStatus.FAILED
        job.stage = "failed"
        job.progress = 1.0
        job.error = failed[0].error if failed else "no clips were rendered"

    job.updated_at = utc_now()
    session.add(job)
    return job


def _clips(session: Session, job_id: int) -> list[Clip]:
    return list(
        session.exec(select(Clip).where(Clip.job_id == job_id).order_by(col(Clip.index))).all()
    )


def _active_task_for_job(session: Session, job_id: int) -> Task | None:
    keys = _job_task_keys(session, job_id)
    if not keys:
        return None
    return session.exec(
        select(Task)
        .where(col(Task.status).in_([TaskStatus.QUEUED, TaskStatus.RUNNING]))
        .where(col(Task.dedupe_key).in_(keys))
    ).first()


def _job_task_keys(session: Session, job_id: int) -> list[str]:
    """Dedupe keys of every task that belongs to this job.

    Keys are structured (`download:job:12`, `render:clip:47`) so a job's work
    can be found without parsing task payloads.
    """
    clip_ids = session.exec(select(col(Clip.id)).where(Clip.job_id == job_id)).all()
    return [f"download:job:{job_id}"] + [f"render:clip:{clip_id}" for clip_id in clip_ids]


def _normalize_title(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())[:200]
    return normalized or None


def _compact_metadata(metadata: dict) -> dict:
    """Keep the handful of extractor fields we actually use.

    Raw yt-dlp output is hundreds of kilobytes per video — storing it whole
    made the jobs table slow to list for no benefit.
    """
    keep = ("title", "duration", "webpage_url", "extractor", "uploader", "upload_date", "thumbnail")
    return {key: metadata.get(key) for key in keep if metadata.get(key) is not None}
