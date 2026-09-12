"""Deciding what one clip contains, for whoever is about to render it.

Two callers need exactly the same answer: the render handler, which turns it
into a file, and the preview endpoint, which turns four seconds of it into
something to look at. Before this they would have had to agree by copying, and
a preview that composes a clip even slightly differently from the render is
worse than no preview — it is a preview of something else.

So the sequence lives here once: pick the transcript for the subtitles, plan
the montage, then lay b-roll, music and effects over it. It ends where ffmpeg
begins.

The database is read in one short call at the front (`library_for`) and never
touched again. Composing a clip can mean transcribing it, which is minutes on
a CPU, and holding a SQLite session open across that is how a single worker
blocks every other one.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Callable

from sqlmodel import Session

from app.adapters.asr import cache as transcript_cache
from app.adapters.asr import selection
from app.adapters.media import compiler
from app.domain import audio as audio_planner
from app.domain import captions as caption_builder
from app.domain import composition as comp
from app.domain import inserts as insert_planner
from app.domain import style as style_module
from app.services import assets

log = logging.getLogger(__name__)

Progress = Callable[[str, float], None]


@dataclasses.dataclass(frozen=True)
class Library:
    """What the b-roll library offers this clip, read in one go."""

    options: list[insert_planner.AssetOption] = dataclasses.field(default_factory=list)
    # Only for a split screen: the video that fills the bottom half.
    companion_path: str | None = None


@dataclasses.dataclass(frozen=True)
class ClipPlan:
    """A clip described and dressed, with a note of where its words came from."""

    composition: comp.Composition
    headline: str
    subtitle_source: str
    companion_path: str | None


def library_for(session: Session, *, style: style_module.StyleSpec, seed: int) -> Library:
    """The library rows this clip could use. The only database read here."""
    wants_assets = style.inserts.enabled or (
        style.audio.enabled and (style.audio.music or style.audio.sfx)
    )
    options = assets.options_for_planner(session) if wants_assets else []

    companion_path = None
    if style.framing.layout == comp.LAYOUT_SPLIT:
        companion_path = assets.companion_for(
            session, tag=style.framing.companion_tag, seed=seed
        )
        if companion_path is None:
            log.warning(
                "a split screen was asked for but the library has no video tagged '%s'",
                style.framing.companion_tag,
            )
    return Library(options=options, companion_path=companion_path)


def compose_clip(
    *,
    clip,
    job,
    options: dict[str, Any],
    style: style_module.StyleSpec,
    library: Library | None = None,
    seed: int | None = None,
    allow_transcription: bool = True,
    on_progress: Progress | None = None,
) -> ClipPlan:
    """Everything between "this clip exists" and "hand it to ffmpeg".

    `allow_transcription=False` keeps this inside a request: it takes the
    words already on disk and never starts a Whisper pass, which a preview
    cannot afford to wait for.
    """
    source_path = options.get("source_path") or job.original_path or ""
    source_title = options.get("source_title") or job.display_title
    shelf = library or Library()
    clip_seed = seed if seed is not None else int(clip.id or 0)

    _report(on_progress, "preparing_subtitles", 0.1)
    subtitle_segments, subtitle_meta = selection.select_subtitle_transcript(
        source_path,
        fallback_segments=cached_segments(source_path, options.get("transcript_settings")),
        start_sec=clip.start_sec,
        end_sec=clip.end_sec,
        enabled=style.subtitles.enabled,
        job_transcript_model=options.get("transcript_settings"),
        allow_transcription=allow_transcription,
    )

    # The burned-in headline mirrors the caption's first line, so what a viewer
    # reads on the video and in the description agree.
    headline = caption_builder.clip_headline(
        source_title=source_title,
        title=clip.title or f"clip {clip.index}",
        index=clip.index,
    )

    _report(on_progress, "planning_montage", 0.2)
    composition = compiler.plan_vertical_clip(
        source_path,
        start_sec=clip.start_sec,
        end_sec=clip.end_sec,
        style=style,
        companion_path=shelf.companion_path,
        transcript_segments=subtitle_segments,
        fallback_subtitle_text=clip.text or "",
        title_text=headline,
    )
    composition = dress(
        composition,
        shelf.options,
        seed=clip_seed,
        broll=style.inserts.enabled,
        music=style.audio.music and style.audio.enabled,
        sfx=style.audio.sfx and style.audio.enabled,
    )

    return ClipPlan(
        composition=composition,
        headline=headline,
        subtitle_source=str(subtitle_meta.get("source") or ""),
        companion_path=shelf.companion_path,
    )


def dress(
    composition: comp.Composition,
    library: list[insert_planner.AssetOption],
    *,
    seed: int,
    broll: bool,
    music: bool,
    sfx: bool,
) -> comp.Composition:
    """Lay b-roll, a music bed and transition sounds over a planned clip.

    All three read the same library and are seeded by the clip rather than by
    chance, so re-rendering produces the same edit — otherwise the fragment
    cache would be inspecting a composition it had never seen before every
    single time.
    """
    if not library or not (broll or music or sfx):
        return composition

    changes: dict = {}
    if broll:
        chosen = insert_planner.choose_inserts(
            composition, assets=library, policy=composition.style.inserts, seed=seed
        )
        if chosen:
            changes["inserts"] = chosen

    if music or sfx:
        policy = composition.style.audio
        # B-roll first, deliberately: an insert appearing is one of the moments
        # a transition sound belongs on, and it does not exist until now.
        staged = dataclasses.replace(composition, **changes) if changes else composition
        if music:
            bed = audio_planner.choose_music(staged, assets=library, policy=policy, seed=seed)
            if bed is not None:
                changes["music"] = bed
        if sfx:
            effects = audio_planner.choose_effects(
                staged, assets=library, policy=policy, seed=seed
            )
            if effects:
                changes["effects"] = effects

    if not changes:
        return composition
    log.info(
        "clip %s takes %d insert(s), %d effect(s) and %s music from a library of %d",
        seed, len(changes.get("inserts", ())), len(changes.get("effects", ())),
        "a" if changes.get("music") else "no", len(library),
    )
    return dataclasses.replace(composition, **changes)


def library_paths(composition: comp.Composition, companion_path: str | None) -> list[str]:
    """Every library file this clip actually used, for the rotation counter."""
    paths = [insert.source_path for insert in composition.inserts]
    paths.extend(effect.source_path for effect in composition.effects)
    if composition.music is not None:
        paths.append(composition.music.source_path)
    if companion_path:
        paths.append(companion_path)
    return paths


def cached_segments(source_path: str, transcript_settings: Any) -> list[dict]:
    """The job transcript, used as the fallback when no per-clip one exists."""
    cached = transcript_cache.load_media_transcript(source_path, settings=transcript_settings)
    if cached:
        return cached[0]
    plain = transcript_cache.load_media_transcript(source_path)
    return plain[0] if plain else []


def _report(on_progress: Progress | None, stage: str, fraction: float) -> None:
    if on_progress is not None:
        on_progress(stage, fraction)
