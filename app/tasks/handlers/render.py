"""Handler: render one clip into a vertical, subtitled video plus a cover.

Renders are independent of each other, which is the whole reason planning and
rendering are separate task kinds: several worker processes can render clips
of the same job in parallel, and one bad clip cannot fail the rest.

What a clip *contains* is decided in `app.services.rendering`, which the
preview endpoint uses as well. This file is the part only a background worker
does: claiming the clip, reporting progress, writing files, recording what came
out.
"""
from __future__ import annotations

import logging
import os

from app.adapters.media import ffmpeg as media
from montage import client as montage
from app.core.errors import PermanentError
from app.db.enums import TaskKind
from app.domain import captions as caption_builder
from montage import composition as comp
from app.domain import profiles
from montage import style as style_module
from app.services import assets, clip_jobs, clips, rendering
from app.tasks.context import TaskContext
from app.tasks.registry import register_handler

log = logging.getLogger(__name__)


def _on_failure(ctx: TaskContext, message: str, final: bool) -> None:
    """Reflect a failed render on the clip — but only once retries are done.

    Marking it failed on the first attempt would show a clip as broken while a
    retry was still queued, and would settle the job's status prematurely.
    """
    if not final:
        return
    clip_id = ctx.get("clip_id")
    if clip_id is None:
        return
    with ctx.db() as session:
        clips.mark_failed(session, int(clip_id), message)


@register_handler(TaskKind.RENDER, on_failure=_on_failure)
def handle_render(ctx: TaskContext) -> dict:
    clip_id = ctx.require_int("clip_id")

    with ctx.db() as session:
        clip = clips.get(session, clip_id)
        job = clip_jobs.get(session, clip.job_id)
        # From the job, not from this task: cutting no longer forwards them,
        # and a re-render queued days later reads the same thing this one does.
        options = _montage_options(ctx, job)
        # Resolved inside the session because a preset lives in the database,
        # and resolved once because everything below reads from it.
        style = style_for(job, options)
        spec = _RenderSpec(
            clip_id=clip_id,
            index=clip.index,
            start_sec=clip.start_sec,
            end_sec=clip.end_sec,
            title=clip.title or f"clip {clip.index}",
            text=clip.text or "",
            source_path=options.get("source_path") or job.original_path or "",
            source_title=options.get("source_title") or job.display_title,
            thumbnail_path=options.get("thumbnail_path") or job.thumbnail_path,
        )
        library = rendering.library_for(session, style=style, seed=clip_id)
        clips.mark_rendering(session, clip_id)

    if not spec.source_path:
        raise PermanentError("the job has no downloaded source to render from")
    if not os.path.isfile(spec.source_path):
        # Named but not there. The retention sweep deletes a job's source a few
        # days after it finishes, and a re-render queued after that arrives
        # here with a path to nothing — as does any job whose file was removed
        # by hand. Saying so now beats three attempts and fifteen minutes of
        # backoff spent on an ffmpeg error about an input it could not open.
        raise PermanentError(
            f"the source file is gone: {spec.source_path}. It was most likely "
            "swept by the retention rules after the job finished — re-run the "
            "job to download it again."
        )

    output_path = media.clip_output_path(
        job_id_of(ctx), index=spec.index, start_sec=spec.start_sec,
        end_sec=spec.end_sec, title=spec.title,
    )

    plan = rendering.compose_clip(
        clip=spec, job=spec, options=options, style=style,
        library=library, seed=clip_id, on_progress=ctx.progress,
    )
    composition = plan.composition

    ctx.progress("rendering_video", 0.25)
    result = montage.render(
        composition,
        output_path,
        strategy=options.get("strategy"),
        source_duration_sec=max(0.0, spec.end_sec - spec.start_sec),
    )

    ctx.progress("rendering_cover", 0.85)
    cover_path = montage.cover(
        source_path=spec.source_path,
        output_path=montage.cover_path_for(result.output_path),
        start_sec=spec.start_sec,
        title_text=caption_builder.display_title(spec.source_title, spec.title),
        part_text=f"часть {spec.index}",
        thumbnail_path=spec.thumbnail_path,
        width=composition.canvas.width,
        height=composition.canvas.height,
    )

    with ctx.db() as session:
        clips.mark_ready(
            session, clip_id,
            video_path=result.output_path,
            cover_path=cover_path,
            composition=comp.to_dict(composition),
        )
        # Recorded only once the clip exists: the planner rotates by last use,
        # and a fragment that never made it into a file was never on screen.
        assets.mark_used(session, rendering.library_paths(composition, plan.companion_path))

    log.info("rendered clip %s -> %s", clip_id, result.output_path)
    return {
        "clip_id": clip_id,
        "path": result.output_path,
        "cover_path": cover_path,
        "output_duration": result.output_duration,
        "strategy": result.strategy,
        "insert_count": len(composition.layers),
        "effect_count": len(composition.effects),
        "music": composition.music.source_path if composition.music else None,
        "layout": sorted(composition.layouts),
        "segment_count": result.segment_count,
        "fragments_reused": result.fragments_reused,
        "subtitle_count": result.subtitle_count,
        "subtitle_source": plan.subtitle_source,
        "silence_removed_seconds": result.silence_removed_seconds,
        "qa": result.qa,
    }


def _montage_options(ctx: TaskContext, job) -> dict:
    """How this clip is dressed: the job's options, with this task's on top.

    Three kinds of task arrive here and the same merge serves all of them. One
    queued by planning carries no options at all and takes the job's. A
    re-render carries the one thing the caller changed, and it wins over the
    job without discarding the rest of it. A task queued by a worker from
    before the job kept its own options carries the lot, and the job has
    nothing to contribute — so the payload is what is left standing.

    The transcript settings go underneath as a default: they are a fact about
    the source rather than an option, and they arrived through the payload
    before only because the cutting stage was the one putting them there.
    """
    options = dict(clip_jobs.montage_options(job))
    options.update(ctx.get("render") or {})
    options.setdefault("transcript_settings", clip_jobs.transcript_settings(job))
    return options


def style_for(job, options: dict) -> style_module.StyleSpec:
    """The look this clip renders with.

    A task queued since styles existed carries the whole thing, already
    resolved when the job started — so the fiftieth clip of a job renders like
    the first even if somebody edited the preset while it was running. A task
    queued before that carries the old flat switches, and they are read as a
    style so a queue that was full during the upgrade still drains correctly.
    """
    profile_layer = profiles.style_overrides(profiles.get(job.profile))
    stored = options.get("style")
    if isinstance(stored, dict) and stored:
        return style_module.resolve(profile=profile_layer, preset=stored)
    return style_module.resolve(profile=profile_layer, overrides=_legacy_overrides(options))


def _legacy_overrides(options: dict) -> dict:
    """The flat render switches of a pre-style task, as a style override."""
    return {
        "framing": {
            "layout": options.get("layout"),
            "companion_tag": options.get("companion_tag") or None,
        },
        "pacing": {"remove_silence": options.get("auto_montage")},
        "inserts": {"enabled": options.get("inserts")},
        "audio": {"music": options.get("music"), "sfx": options.get("sfx")},
        "subtitles": {
            "enabled": options.get("burn_subtitles"),
            "font_size": options.get("subtitle_font_size"),
            "position_percent": options.get("subtitle_position_percent"),
        },
        "delivery": {
            "width": options.get("width"),
            "height": options.get("height"),
            "crf": options.get("crf"),
        },
    }


class _RenderSpec:
    """The clip and its job, read once so the session can be let go.

    Composing a clip can mean transcribing it, which is minutes on a CPU.
    Holding a SQLite session open across that is how one worker blocks every
    other one, so everything needed downstream is copied out first — which is
    also why this stands in for both the clip and the job that
    `rendering.compose_clip` asks for.
    """

    __slots__ = (
        "clip_id", "index", "start_sec", "end_sec", "title", "text",
        "source_path", "source_title", "thumbnail_path",
    )

    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)

    @property
    def id(self) -> int:
        return self.clip_id

    @property
    def original_path(self) -> str:
        return self.source_path

    @property
    def display_title(self) -> str:
        return self.source_title


def job_id_of(ctx: TaskContext) -> int:
    return int(ctx.get("job_id") or 0)
