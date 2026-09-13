"""AUT's half of composing a clip: the words, the headline, the library.

Two callers need exactly the same answer: the render handler, which turns it
into a file, and the preview endpoint, which turns four seconds of it into
something to look at. A preview that composes a clip even slightly differently
from the render is worse than no preview — it is a preview of something else —
so the sequence lives here once.

What is left here after the montage service moved out is the part that is
AUT's by rights: picking the transcript (ASR belongs to the cutter that needs
it), composing the headline (it has to agree with the caption the clip is
published under), and reading the library. Those three become facts, and
`montage.client` takes them from there.

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
from app.domain import captions as caption_builder
from app.services import assets
from montage import client as montage
from montage import composition as comp
from montage import style as style_module
from montage.rules import inserts as insert_planner

log = logging.getLogger(__name__)

Progress = Callable[[str, float], None]


# The shape the montage service takes its library in. Aliased rather than
# restated, so there is one definition of what a clip is offered.
Library = montage.Library


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
    plan = montage.compose(montage.ClipRequest(
        source_path=source_path,
        start_sec=clip.start_sec,
        end_sec=clip.end_sec,
        style=style,
        speech=tuple(subtitle_segments),
        fallback_text=clip.text or "",
        title_text=headline,
        library=shelf,
        seed=clip_seed,
    ))

    return ClipPlan(
        composition=plan.composition,
        headline=headline,
        subtitle_source=str(subtitle_meta.get("source") or ""),
        companion_path=shelf.companion_path,
    )


def library_paths(composition: comp.Composition, companion_path: str | None) -> list[str]:
    """Every library file this clip actually used, for the rotation counter."""
    paths = [layer.source_path for layer in composition.layers]
    paths.extend(track.source_path for track in composition.audio)
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
