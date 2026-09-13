"""Handler: delete media that is no longer needed.

This exists because the previous version had none: the working tree had grown
to 11 GB of source downloads and intermediate slices, none of which anything
would ever read again.

What gets deleted, and only when retention is enabled:

* source downloads of jobs that finished more than `originals_days` ago —
  re-slicing after that is rare and re-downloading is cheap next to the disk;
* rendered clips that were published more than `published_clips_days` ago —
  the video lives on TikTok now;
* everything belonging to jobs older than `jobs_days`;
* yt-dlp's leftovers from downloads that never finished, once they are older
  than `abandoned_download_hours` and no retry could still resume from them,
  and the per-thread cookie jars its workers leave in the cookies directory;
* the oldest rendered segments, once the fragment cache is over its size cap.

Database rows are kept. They are tiny, and they are the record of what was
posted; only the files go.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import timedelta

from sqlmodel import col, select

from montage import client as montage
from app.adapters.media.ffmpeg import media_root
from app.core.clock import utc_now
from app.core.config import get_settings
from app.db.enums import ClipStatus, JobStatus, PublicationStatus, TaskKind
from app.db.models import Clip, ClipJob, Publication, Task
from app.services import clips as clip_service
from app.tasks import queue
from app.tasks.context import TaskContext
from app.tasks.registry import register_handler

log = logging.getLogger(__name__)


@register_handler(TaskKind.CLEANUP)
def handle_cleanup(ctx: TaskContext) -> dict:
    retention = get_settings().retention
    if not retention.enabled:
        return {"skipped": "retention is disabled"}

    now = utc_now()
    removed_originals = 0
    removed_clips = 0
    freed_bytes = 0

    ctx.progress("removing_sources", 0.2)
    with ctx.db() as session:
        cutoff = now - timedelta(days=retention.originals_days)
        stale_jobs = session.exec(
            select(ClipJob)
            .where(col(ClipJob.status).in_([JobStatus.READY, JobStatus.FAILED, JobStatus.CANCELLED]))
            .where(col(ClipJob.updated_at) < cutoff)
            .where(col(ClipJob.original_path) != None)  # noqa: E711
        ).all()
        removed, freed = _remove_sources(session, stale_jobs)
        removed_originals += removed
        freed_bytes += freed

    ctx.progress("removing_published_clips", 0.6)
    with ctx.db() as session:
        cutoff = now - timedelta(days=retention.published_clips_days)
        published = session.exec(
            select(Clip)
            .join(Publication, col(Publication.clip_id) == col(Clip.id))
            .where(Publication.status == PublicationStatus.PUBLISHED)
            .where(col(Publication.published_at) < cutoff)
            .where(Clip.status == ClipStatus.READY)
            .where(col(Clip.video_path) != None)  # noqa: E711
        ).all()
        for clip in published:
            freed_bytes += _size(clip.video_path) + _size(clip.cover_path)
            removed_clips += clip_service.delete_files(clip)
            clip.video_path = None
            clip.cover_path = None
            session.add(clip)

    ctx.progress("removing_expired_jobs", 0.9)
    with ctx.db() as session:
        cutoff = now - timedelta(days=retention.jobs_days)
        expired = session.exec(select(ClipJob).where(col(ClipJob.updated_at) < cutoff)).all()
        removed, freed = _remove_sources(session, expired)
        removed_originals += removed
        freed_bytes += freed
        for job in expired:
            for clip in session.exec(select(Clip).where(Clip.job_id == job.id)).all():
                if not clip.video_path:
                    continue
                freed_bytes += _size(clip.video_path) + _size(clip.cover_path)
                removed_clips += clip_service.delete_files(clip)
                clip.video_path = None
                clip.cover_path = None
                session.add(clip)

    ctx.progress("removing_abandoned_downloads", 0.93)
    abandoned, abandoned_bytes = sweep_abandoned_downloads(retention.abandoned_download_hours)
    removed_originals += abandoned
    freed_bytes += abandoned_bytes

    ctx.progress("removing_stale_cookie_jars", 0.94)
    jars, jar_bytes = sweep_stale_cookie_jars(retention.abandoned_download_hours)
    freed_bytes += jar_bytes

    ctx.progress("trimming_fragment_cache", 0.95)
    evicted, evicted_bytes = trim_fragment_cache(retention.fragment_cache_gb)
    freed_bytes += evicted_bytes

    summary = {
        "removed_originals": removed_originals,
        "removed_clip_files": removed_clips,
        "evicted_fragments": evicted,
        "freed_megabytes": round(freed_bytes / 1024 / 1024, 1),
    }
    _schedule_next(ctx)
    log.info("cleanup freed %.1f MB", summary["freed_megabytes"])
    return summary


# One periodic sweep at a time. `ensure_scheduled` and `_schedule_next` share
# the key, so a restart cannot start a second chain running alongside the first.
PERIODIC_KEY = "cleanup:periodic"


def ensure_scheduled(session) -> None:
    """Make sure a periodic sweep is on the queue. Safe to call repeatedly.

    Called when the API starts, which is what gets the chain going the first
    time and what restarts it if a sweep ever ends without queueing its
    successor. The dedupe key makes a second call a no-op while one is
    pending.
    """
    queue.enqueue(
        session,
        TaskKind.CLEANUP,
        {},
        run_at=utc_now() + timedelta(minutes=5),
        dedupe_key=PERIODIC_KEY,
    )


def _schedule_next(ctx: TaskContext) -> None:
    interval = max(0.25, get_settings().retention.cleanup_interval_hours)
    with ctx.db() as session:
        current = session.get(Task, ctx.task_id)
        if current is not None:
            # This task still holds the key while it runs, and `enqueue` hands
            # back a task that is still active rather than making a new one.
            queue.release_dedupe_key(session, current)
            session.flush()
        queue.enqueue(
            session,
            TaskKind.CLEANUP,
            {},
            run_at=utc_now() + timedelta(hours=interval),
            dedupe_key=PERIODIC_KEY,
        )


def trim_fragment_cache(limit_gb: float) -> tuple[int, int]:
    """Drop the oldest rendered segments until the cache fits its cap.

    Bounded by size rather than age because that is what the cache actually
    costs: a fragment stays useful for as long as its clip might be re-rendered,
    which is not a number of days. Modification time stands in for last use —
    a cache hit touches the file, and access times are unreliable on Windows
    and on network filesystems.

    Returns (files removed, bytes freed).
    """
    directory, suffix = montage.fragment_cache()
    entries: list[tuple[float, int, str]] = []
    total = 0
    with os.scandir(directory) as listing:
        for entry in listing:
            if not entry.is_file() or not entry.name.endswith(suffix):
                continue
            try:
                stat = entry.stat()
            except OSError:  # pragma: no cover - vanished mid-sweep
                continue
            entries.append((stat.st_mtime, stat.st_size, entry.path))
            total += stat.st_size

    limit = max(0.0, limit_gb) * 1024**3
    if total <= limit:
        return 0, 0

    removed = 0
    freed = 0
    for _, size, path in sorted(entries):
        if total - freed <= limit:
            break
        cleared, _ = _remove(path)
        if cleared:
            removed += 1
            freed += size
    log.info("evicted %d cached fragment(s), freeing %.1f MB", removed, freed / 1024 / 1024)
    return removed, freed


def _remove_sources(session, jobs: list[ClipJob]) -> tuple[int, int]:
    """Delete these jobs' source downloads, sparing any that are still shared.

    Two jobs pointing at one file is normal: re-running the same video reuses
    the download instead of fetching half a gigabyte again — see
    `clip_jobs.source_downloaded_elsewhere`. So a path only goes when every
    job holding it is in this sweep; otherwise the survivor is left with an
    `original_path` to a file that no longer exists, and its next render dies
    looking for it.

    Returns (files removed, bytes freed).
    """
    owners: dict[str, list[ClipJob]] = {}
    for job in jobs:
        if job.original_path:
            owners.setdefault(job.original_path, []).append(job)

    removed = 0
    freed = 0
    for path, holders in owners.items():
        still_wanted = session.exec(
            select(ClipJob.id)
            .where(ClipJob.original_path == path)
            .where(col(ClipJob.id).not_in([job.id for job in holders]))
        ).first()
        if still_wanted is not None:
            continue
        existed = os.path.exists(path)
        cleared, size = _remove(path)
        if not cleared:
            # Still on disk and undeletable — in use, or a permissions
            # problem. The rows are right to keep pointing at it.
            continue
        if existed:
            freed += size
            removed += 1
        # Cleared covers a file that had already vanished, and that is the
        # case worth catching: the row went on naming a path nothing could
        # read, every later sweep skipped it for freeing no bytes, and the
        # job's next render died looking for it — the exact failure this
        # function's docstring promises not to leave behind.
        for job in holders:
            job.original_path = None
            session.add(job)
    return removed, freed


def sweep_abandoned_downloads(max_age_hours: float) -> tuple[int, int]:
    """Delete yt-dlp's leftovers from downloads that never finished.

    A failed or abandoned download leaves `.part`, `.part-FragN.part` and
    `.ytdl` files, plus format intermediates (`job-7.f399.mp4`) written before
    the merge that never happened. Nothing ever reads them again — a retry
    resumes from the `.part` only while the task is still alive — and no rule
    above touches them, because they were never any job's `original_path`.
    Found here as 63 MB from two jobs that failed three days earlier.

    The age floor is what keeps this from deleting a download in progress.

    Returns (files removed, bytes freed).
    """
    directory = os.path.join(media_root(), "originals")
    if not os.path.isdir(directory):
        return 0, 0
    cutoff = time.time() - max(1.0, max_age_hours) * 3600
    removed = 0
    freed = 0
    with os.scandir(directory) as listing:
        for entry in listing:
            if not entry.is_file() or not _ABANDONED.search(entry.name):
                continue
            try:
                if entry.stat().st_mtime > cutoff:
                    continue
            except OSError:  # pragma: no cover - vanished mid-sweep
                continue
            cleared, size = _remove(entry.path)
            if cleared:
                removed += 1
                freed += size
    if removed:
        log.info("removed %d abandoned download file(s), freeing %.1f MB",
                 removed, freed / 1024 / 1024)
    return removed, freed


def sweep_stale_cookie_jars(max_age_hours: float) -> tuple[int, int]:
    """Delete the per-thread yt-dlp cookie jars nothing is using any more.

    `options._writable_cookie_jar` copies the read-only YouTube export to
    `ytdlp-jar.<pid>.<thread>.txt` so yt-dlp can save its jar back without
    touching the original. The thread id is new on every worker start and
    nothing ever removed the old ones: seventeen of them had piled up in the
    directory that also holds the TikTok sessions.

    Age is the only safe test — a jar in use is rewritten at the end of every
    download, so anything older than a download could possibly take is dead.

    Returns (files removed, bytes freed).
    """
    directory = get_settings().paths.cookies_dir
    if not directory.is_dir():
        return 0, 0
    cutoff = time.time() - max(1.0, max_age_hours) * 3600
    removed = 0
    freed = 0
    for path in directory.glob("ytdlp-jar.*.txt"):
        try:
            if path.stat().st_mtime > cutoff:
                continue
        except OSError:  # pragma: no cover - vanished mid-sweep
            continue
        cleared, size = _remove(str(path))
        if cleared:
            removed += 1
            freed += size
    if removed:
        log.info("removed %d stale yt-dlp cookie jar(s)", removed)
    return removed, freed


# `.part`, `.part-Frag12.part`, `.ytdl`, and the `job-7.f399.mp4` intermediates
# yt-dlp writes per format before merging them.
_ABANDONED = re.compile(r"(\.part(-Frag\d+\.part)?|\.ytdl|\.f\d+\.[A-Za-z0-9]+)$")


def _size(path: str | None) -> int:
    if path and os.path.exists(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    return 0


def _remove(path: str | None) -> tuple[bool, int]:
    """Delete a file. Returns (the path is clear now, bytes freed).

    Two things the previous "return the bytes freed" could not say. A file
    that is already gone leaves the path just as clear as deleting it does,
    and callers need that to decide whether a row may stop pointing at it. And
    an empty file is a file: sizing the result meant a zero-byte `.part`
    survived every sweep it ever appeared in, because no bytes freed read as
    nothing was there.
    """
    if not path or not os.path.exists(path):
        return bool(path), 0
    size = _size(path)
    try:
        os.remove(path)
        return True, size
    except OSError as exc:  # pragma: no cover - filesystem edge case
        log.warning("could not delete %s: %s", path, exc)
        return False, 0
