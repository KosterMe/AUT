"""Handler: fetch a source video, transcribe it, and plan its clips.

The expensive, sequential part of the pipeline. It ends by queueing one render
task per clip, which is where parallelism comes from — renders are independent
and several worker processes can chew through them at once.
"""
from __future__ import annotations

import logging
import os

from app.adapters.media import ffmpeg as media
from app.adapters.media import scenes as scene_probe
from app.adapters.youtube import downloader
from app.core.errors import PermanentError
from app.db.enums import JobStatus, TaskKind
from app.domain import cutting, profiles
from app.services import clip_jobs, clips, transcripts
from app.tasks.context import TaskContext
from app.tasks.registry import register_handler

log = logging.getLogger(__name__)

DEFAULT_MIN_CLIP_SECONDS = 90.0
DEFAULT_MAX_CLIP_SECONDS = 120.0


def _on_failure(ctx: TaskContext, message: str, final: bool) -> None:
    """Surface a failed download on the job — including one that will retry.

    A retry is not a non-event for the job: the backoff is minutes long, and a
    job that says nothing during it looks exactly like one no worker ever
    claimed.
    """
    job_id = ctx.get("job_id")
    if job_id is None:
        return
    with ctx.db() as session:
        if final:
            clip_jobs.fail(session, int(job_id), message)
        else:
            clip_jobs.set_retrying(session, int(job_id), message, attempt=ctx.attempt)


@register_handler(TaskKind.DOWNLOAD, on_failure=_on_failure)
def handle_download(ctx: TaskContext) -> dict:
    job_id = ctx.require_int("job_id")

    with ctx.db() as session:
        job = clip_jobs.get(session, job_id)
        source_ref, platform = job.source_ref, job.source_platform
        existing_path = job.original_path
        # The job says "queued" until something says otherwise, and fetching a
        # source is the longest step in the pipeline. Claim it on the job now,
        # not after the download returns.
        clip_jobs.set_progress(session, job_id, JobStatus.DOWNLOADING, "fetching source", 0.05)

    ctx.progress("resolving_source", 0.05)
    media_path, metadata = _resolve_source(
        job_id,
        source_ref=source_ref,
        platform=platform,
        existing_path=existing_path,
        on_progress=_source_progress(ctx, job_id),
    )

    duration = media.ffprobe_duration(media_path) or _as_float(metadata.get("duration")) or 0.0
    thumbnail = media.prepare_source_thumbnail(job_id, media.best_thumbnail_url(metadata))

    with ctx.db() as session:
        clip_jobs.record_source(
            session,
            job_id,
            original_path=media_path,
            duration_seconds=duration,
            title=metadata.get("title"),
            thumbnail_path=thumbnail,
            metadata=metadata,
        )
        clip_jobs.set_progress(session, job_id, JobStatus.TRANSCRIBING, "transcribing", 0.15)

    profile = profiles.get(ctx.get("profile"))
    cutter = str(ctx.get("cutter") or profile.cutter)

    transcript = None
    if profile.requires_transcript or cutter == cutting.CUTTER_SPEECH:
        ctx.progress("transcribing", 0.2)
        transcript = transcripts.load_or_build(
            media_path,
            source_ref=source_ref,
            metadata=metadata,
            duration=duration,
            on_progress=ctx.progress,
        )
        if not transcript.segments:
            # The speech cutter has nothing to work with, and a fixed-length cut
            # would chop sentences. Say which profile does not need one rather
            # than only that this one failed.
            raise PermanentError(
                "no transcript could be produced — the video may have no speech, or "
                "the ASR backend is misconfigured. Videos without dialogue should use "
                "the 'film' profile, which cuts on scene changes instead."
            )

    ctx.progress("planning_clips", 0.85)
    with ctx.db() as session:
        clip_jobs.set_progress(session, job_id, JobStatus.PLANNING, "planning clips", 0.85)
        job = clip_jobs.get(session, job_id)
        source_title = job.display_title

    specs = _cut(
        ctx,
        cutter=cutter,
        media_path=media_path,
        duration=duration,
        transcript=transcript,
    )
    if not specs:
        raise PermanentError(f"the {cutter} cutter produced no usable clips")

    render_options = dict(ctx.get("render") or {})
    render_options.setdefault("source_path", media_path)
    render_options.setdefault("source_title", source_title)
    render_options.setdefault("thumbnail_path", thumbnail)
    if transcript is not None:
        render_options.setdefault("transcript_settings", transcript.settings)

    with ctx.db() as session:
        created = clips.plan(session, job_id, specs, render_options=render_options)
        clip_jobs.set_progress(session, job_id, JobStatus.RENDERING, "rendering", 0.0)
        clip_ids = [clip.id for clip in created]

    log.info(
        "job %s planned %d clip(s) from a %.0fs source with the %s cutter",
        job_id, len(clip_ids), duration, cutter,
    )
    return {
        "clip_ids": clip_ids,
        "clip_count": len(clip_ids),
        "duration_seconds": duration,
        "profile": profile.name,
        "cutter": cutter,
        "transcript_source": transcript.source if transcript else None,
        "source_path": media_path,
    }


def _cut(ctx: TaskContext, *, cutter: str, media_path: str, duration: float, transcript):
    """Run the profile's cutter over the source.

    The scene cutter is the only one that costs anything here — one decode of
    the source, about a thirty-seventh of its running time — and it is only
    reached by profiles that asked for it.
    """
    minimum = float(ctx.get("min_clip_seconds") or DEFAULT_MIN_CLIP_SECONDS)
    maximum = float(ctx.get("max_clip_seconds") or DEFAULT_MAX_CLIP_SECONDS)
    gap = float(ctx.get("gap_seconds") or cutting.DEFAULT_GAP_SECONDS)
    max_clips = int(ctx.get("max_clips") or 0)

    if cutter == cutting.CUTTER_SCENES:
        ctx.progress("finding_scenes", 0.6)
        analysis = scene_probe.analyse(media_path)
        if not analysis.ok:
            log.warning("scene analysis failed (%s); cutting by the clock", analysis.detail)
        return cutting.build_scene_slices(
            source_duration=duration,
            scene_changes=analysis.scene_changes,
            loudness=analysis.loudness,
            min_clip_seconds=minimum,
            max_clip_seconds=maximum,
            gap_seconds=gap,
            max_clips=max_clips,
        )

    if cutter == cutting.CUTTER_PLAIN or transcript is None:
        return cutting.build_plain_slices(
            source_duration=duration,
            clip_seconds=maximum,
            gap_seconds=gap,
            max_clips=max_clips,
        )

    return cutting.build_semantic_slices(
        transcript.segments,
        source_duration=duration,
        min_clip_seconds=minimum,
        max_clip_seconds=maximum,
        gap_seconds=gap,
        max_clips=max_clips,
    )


def _source_progress(ctx: TaskContext, job_id: int):
    """Report a download's own progress onto both the task and the job.

    Fetching the source is the longest step and, before this, the only silent
    one: a job that needed twenty minutes to pull a video looked exactly like a
    job that had died at 5%.
    """

    # 0.05 to 0.15 — the slice of the job this step is worth. Transcription and
    # planning own what comes after, and their numbers must keep rising.
    def report(fraction: float) -> None:
        value = 0.05 + 0.10 * fraction
        with ctx.db() as session:
            clip_jobs.set_progress(
                session, job_id, JobStatus.DOWNLOADING, f"downloading {fraction:.0%}", value
            )
        # Checks for cancellation too, so a download can now be stopped while
        # it runs rather than only at the step boundaries around it.
        ctx.progress("downloading_source", value)

    return report


def _resolve_source(
    job_id: int,
    *,
    source_ref: str,
    platform: str,
    existing_path: str | None,
    on_progress=None,
) -> tuple[str, dict]:
    """Return a local path to the source, downloading it only if needed."""
    if existing_path and os.path.exists(existing_path):
        log.info("reusing already-downloaded source %s", existing_path)
        metadata = {
            "title": None,
            "duration": media.ffprobe_duration(existing_path),
            "webpage_url": source_ref,
            "extractor": "cached",
        }
        if platform != "local":
            # The file is here but its name is not; fetch just the metadata so
            # captions keep saying what the video is called.
            metadata.update(
                {k: v for k, v in downloader.title_for_cached_source(source_ref).items() if v}
            )
        return existing_path, metadata

    if platform == "local":
        path = source_ref if os.path.isabs(source_ref) else os.path.abspath(source_ref)
        if not os.path.exists(path):
            raise PermanentError(f"local source file does not exist: {path}")
        return path, {
            "title": os.path.basename(path),
            "duration": media.ffprobe_duration(path),
            "webpage_url": path,
            "extractor": "local",
        }

    return downloader.download_source(source_ref, job_id=job_id, on_progress=on_progress)


def _as_float(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
