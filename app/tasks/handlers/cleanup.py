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
* the oldest rendered segments, once the fragment cache is over its size cap.

Database rows are kept. They are tiny, and they are the record of what was
posted; only the files go.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from sqlmodel import col, select

from app.adapters.media import compiler
from app.core.clock import utc_now
from app.core.config import get_settings
from app.db.enums import ClipStatus, JobStatus, PublicationStatus, TaskKind
from app.db.models import Clip, ClipJob, Publication
from app.services import clips as clip_service
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
        for job in stale_jobs:
            freed = _remove(job.original_path)
            if freed:
                freed_bytes += freed
                removed_originals += 1
                job.original_path = None
                session.add(job)

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
        for job in session.exec(select(ClipJob).where(col(ClipJob.updated_at) < cutoff)).all():
            freed_bytes += _remove(job.original_path)
            if job.original_path:
                removed_originals += 1
                job.original_path = None
                session.add(job)
            for clip in session.exec(select(Clip).where(Clip.job_id == job.id)).all():
                if not clip.video_path:
                    continue
                freed_bytes += _size(clip.video_path) + _size(clip.cover_path)
                removed_clips += clip_service.delete_files(clip)
                clip.video_path = None
                clip.cover_path = None
                session.add(clip)

    ctx.progress("trimming_fragment_cache", 0.95)
    evicted, evicted_bytes = trim_fragment_cache(retention.fragment_cache_gb)
    freed_bytes += evicted_bytes

    summary = {
        "removed_originals": removed_originals,
        "removed_clip_files": removed_clips,
        "evicted_fragments": evicted,
        "freed_megabytes": round(freed_bytes / 1024 / 1024, 1),
    }
    log.info("cleanup freed %.1f MB", summary["freed_megabytes"])
    return summary


def trim_fragment_cache(limit_gb: float) -> tuple[int, int]:
    """Drop the oldest rendered segments until the cache fits its cap.

    Bounded by size rather than age because that is what the cache actually
    costs: a fragment stays useful for as long as its clip might be re-rendered,
    which is not a number of days. Modification time stands in for last use —
    a cache hit touches the file, and access times are unreliable on Windows
    and on network filesystems.

    Returns (files removed, bytes freed).
    """
    directory = compiler.fragment_cache_dir()
    entries: list[tuple[float, int, str]] = []
    total = 0
    with os.scandir(directory) as listing:
        for entry in listing:
            if not entry.is_file() or not entry.name.endswith(compiler.FRAGMENT_SUFFIX):
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
        if _remove(path):
            removed += 1
            freed += size
    log.info("evicted %d cached fragment(s), freeing %.1f MB", removed, freed / 1024 / 1024)
    return removed, freed


def _size(path: str | None) -> int:
    if path and os.path.exists(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    return 0


def _remove(path: str | None) -> int:
    """Delete a file, returning how many bytes it freed."""
    size = _size(path)
    if not size:
        return 0
    try:
        os.remove(path)
        return size
    except OSError as exc:  # pragma: no cover - filesystem edge case
        log.warning("could not delete %s: %s", path, exc)
        return 0
