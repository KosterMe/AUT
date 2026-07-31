"""Handler: render one clip into a vertical, subtitled video plus a cover.

Renders are independent of each other, which is the whole reason planning and
rendering are separate task kinds: several worker processes can render clips
of the same job in parallel, and one bad clip cannot fail the rest.
"""
from __future__ import annotations

import dataclasses
import logging

from app.adapters.asr import cache as transcript_cache
from app.adapters.asr import selection
from app.adapters.media import compiler
from app.adapters.media import ffmpeg as media
from app.core.config import get_settings
from app.core.errors import PermanentError
from app.db.enums import TaskKind
from app.domain import audio as audio_planner
from app.domain import captions as caption_builder
from app.domain import composition as comp
from app.domain import inserts as insert_planner
from app.domain import profiles
from app.services import assets, clip_jobs, clips
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
    options = dict(ctx.get("render") or {})

    with ctx.db() as session:
        clip = clips.get(session, clip_id)
        job = clip_jobs.get(session, clip.job_id)
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
        clips.mark_rendering(session, clip_id)

    if not spec.source_path:
        raise PermanentError("the job has no downloaded source to render from")

    settings = get_settings()
    width = int(options.get("width", settings.render.width))
    height = int(options.get("height", settings.render.height))
    crf = int(options.get("crf", settings.render.crf))
    burn_subtitles = bool(options.get("burn_subtitles", True))

    ctx.progress("preparing_subtitles", 0.1)
    subtitle_segments, subtitle_meta = selection.select_subtitle_transcript(
        spec.source_path,
        fallback_segments=_cached_segments(spec.source_path, options.get("transcript_settings")),
        start_sec=spec.start_sec,
        end_sec=spec.end_sec,
        enabled=burn_subtitles,
        job_transcript_model=options.get("transcript_settings"),
    )

    # The burned-in headline mirrors the caption's first line, so what a viewer
    # reads on the video and in the description agree.
    headline = caption_builder.clip_headline(
        source_title=spec.source_title, title=spec.title, index=spec.index
    )
    output_path = media.clip_output_path(
        job_id_of(ctx), index=spec.index, start_sec=spec.start_sec,
        end_sec=spec.end_sec, title=spec.title,
    )

    ctx.progress("planning_montage", 0.2)
    layout = str(options.get("layout") or compiler.LAYOUT_AUTO)
    companion_path = None
    if layout == comp.LAYOUT_SPLIT:
        with ctx.db() as session:
            companion_path = assets.companion_for(
                session,
                tag=str(options.get("companion_tag") or profiles.BACKGROUND_TAG),
                seed=clip_id,
            )
        if companion_path is None:
            log.warning(
                "clip %s wants a split screen but the library has no video tagged '%s'",
                clip_id, options.get("companion_tag") or profiles.BACKGROUND_TAG,
            )

    composition = compiler.plan_vertical_clip(
        spec.source_path,
        start_sec=spec.start_sec,
        end_sec=spec.end_sec,
        width=width,
        height=height,
        crf=crf,
        burn_subtitles=burn_subtitles,
        auto_montage=bool(options.get("auto_montage", False)),
        layout=layout,
        companion_path=companion_path,
        transcript_segments=subtitle_segments,
        fallback_subtitle_text=spec.text,
        subtitle_font_size=int(options.get("subtitle_font_size", settings.subtitles.font_size)),
        subtitle_position_percent=int(
            options.get("subtitle_position_percent", settings.subtitles.position_percent)
        ),
        title_text=headline,
    )
    wants_broll = bool(options.get("inserts", settings.inserts.enabled))
    wants_music = bool(options.get("music", False)) and settings.audio.enabled
    wants_sfx = bool(options.get("sfx", False)) and settings.audio.enabled
    if wants_broll or wants_music or wants_sfx:
        with ctx.db() as session:
            library = assets.options_for_planner(session)
        composition = _dress(
            composition, library, clip_id=clip_id,
            broll=wants_broll, music=wants_music, sfx=wants_sfx,
        )

    ctx.progress("rendering_video", 0.25)
    result = compiler.render(
        composition,
        output_path,
        strategy=options.get("strategy"),
        source_duration_sec=max(0.0, spec.end_sec - spec.start_sec),
    )

    ctx.progress("rendering_cover", 0.85)
    cover_path = media.render_clip_cover(
        source_path=spec.source_path,
        output_path=media.cover_path_for(result.output_path),
        start_sec=spec.start_sec,
        title_text=caption_builder.display_title(spec.source_title, spec.title),
        part_text=f"часть {spec.index}",
        thumbnail_path=spec.thumbnail_path,
        width=width,
        height=height,
    )

    with ctx.db() as session:
        clips.mark_ready(session, clip_id, video_path=result.output_path, cover_path=cover_path)
        # Recorded only once the clip exists: the planner rotates by last use,
        # and a fragment that never made it into a file was never on screen.
        assets.mark_used(session, _library_paths(composition, companion_path))

    log.info("rendered clip %s -> %s", clip_id, result.output_path)
    return {
        "clip_id": clip_id,
        "path": result.output_path,
        "cover_path": cover_path,
        "output_duration": result.output_duration,
        "strategy": result.strategy,
        "insert_count": len(composition.inserts),
        "effect_count": len(composition.effects),
        "music": composition.music.source_path if composition.music else None,
        "layout": sorted(composition.layouts),
        "segment_count": result.segment_count,
        "fragments_reused": result.fragments_reused,
        "subtitle_count": result.subtitle_count,
        "subtitle_source": subtitle_meta.get("source"),
        "silence_removed_seconds": result.silence_removed_seconds,
        "qa": result.qa,
    }


def _dress(composition, library, *, clip_id: int, broll: bool, music: bool, sfx: bool):
    """Lay b-roll, a music bed and transition sounds over a planned clip.

    All three read the same library and are seeded by the clip id rather than
    by chance, so re-rendering produces the same edit — otherwise the fragment
    cache would be inspecting a composition it had never seen before every
    single time.
    """
    if not library:
        return composition

    changes: dict = {}
    if broll:
        chosen = insert_planner.choose_inserts(
            composition, assets=library,
            policy=insert_planner.InsertPolicy.from_settings(), seed=clip_id,
        )
        if chosen:
            changes["inserts"] = chosen

    if music or sfx:
        policy = audio_planner.AudioPolicy.from_settings()
        # B-roll first, deliberately: an insert appearing is one of the moments
        # a transition sound belongs on, and it does not exist until now.
        staged = dataclasses.replace(composition, **changes) if changes else composition
        if music:
            bed = audio_planner.choose_music(
                staged, assets=library, policy=policy, seed=clip_id
            )
            if bed is not None:
                changes["music"] = bed
        if sfx:
            effects = audio_planner.choose_effects(
                staged, assets=library, policy=policy, seed=clip_id
            )
            if effects:
                changes["effects"] = effects

    if not changes:
        return composition
    log.info(
        "clip %s takes %d insert(s), %d effect(s) and %s music from a library of %d",
        clip_id, len(changes.get("inserts", ())), len(changes.get("effects", ())),
        "a" if changes.get("music") else "no", len(library),
    )
    return dataclasses.replace(composition, **changes)


def _library_paths(composition, companion_path: str | None) -> list[str]:
    """Every library file this clip actually used, for the rotation counter."""
    paths = [insert.source_path for insert in composition.inserts]
    paths.extend(effect.source_path for effect in composition.effects)
    if composition.music is not None:
        paths.append(composition.music.source_path)
    if companion_path:
        paths.append(companion_path)
    return paths


class _RenderSpec:
    __slots__ = (
        "clip_id", "index", "start_sec", "end_sec", "title", "text",
        "source_path", "source_title", "thumbnail_path",
    )

    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


def job_id_of(ctx: TaskContext) -> int:
    return int(ctx.get("job_id") or 0)


def _cached_segments(source_path: str, transcript_settings) -> list[dict]:
    """The job transcript, used as the fallback when no per-clip one exists."""
    cached = transcript_cache.load_media_transcript(source_path, settings=transcript_settings)
    if cached:
        return cached[0]
    plain = transcript_cache.load_media_transcript(source_path)
    return plain[0] if plain else []
