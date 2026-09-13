"""What AUT calls. The seam, in process.

Everything crossing this module is a value: paths, a style, a window, the facts
about a clip. Nothing crossing it is a session, a job row or a task. That is the
whole test of whether the boundary was drawn in the right place, and it is
cheaper to fail it here than after the same calls have become HTTP (§13.3).

The division of labour is the one §3.4 sets out. AUT decides *that* a clip
exists and what it is about — where it was cut, what it is called, which words
are spoken in it. This service decides what it looks like. So the transcript
arrives as a fact rather than being fetched: ASR belongs to the cutter, which
needs it for its own work, and a renderer that started Whisper on its own would
be the coupling this split exists to remove.

Files do not cross either. Both sides mount the same media volume and what
travels is a path inside it (§2.4) — a clip's source can be tens of gigabytes,
and moving it between two halves of one machine is the most expensive way to
gain nothing.
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from montage import composition as comp
from montage import style as style_module
from montage import subtitles as subtitle_builder
from montage.render import capabilities as build_capabilities
from montage.render import compiler
from montage import scenario
from montage.render import probe
from montage.rules import audio as audio_planner
from montage.rules import inserts as insert_planner
from montage.scenario import builtin
from montage.scenario import model as scenario_model

log = logging.getLogger(__name__)

Progress = Callable[[str, float], None]


@dataclass(frozen=True)
class Library:
    """What the asset library offers this clip, read in one go.

    A list of values rather than a query: the library is AUT's table today and
    this service's own after §2.5, and either way the renderer is handed rows
    rather than a way to fetch them. Holding a database session across a render
    is how one worker blocks every other one.
    """

    options: list[insert_planner.AssetOption] = field(default_factory=list)
    # Split screen only: the video that fills the bottom half.
    companion_path: str | None = None


@dataclass(frozen=True)
class ClipRequest:
    """One clip to dress: where it is, how it should look, what is said in it."""

    source_path: str
    start_sec: float
    end_sec: float
    style: style_module.StyleSpec
    # Word-timed transcript lines for this clip's window. A fact AUT supplies,
    # because AUT is the side that has a reason to run ASR.
    speech: tuple[Any, ...] = ()
    # Shown when there are no word timings to draw cues from.
    fallback_text: str = ""
    # Burned into the frame. AUT composes it, because it has to agree with the
    # caption the clip is published under, and captions are AUT's.
    title_text: str = ""
    library: Library = field(default_factory=Library)
    seed: int = 0
    index: int = 1
    # Which montage to apply. A built-in by name today; a stored scenario once
    # jobs carry a `scenario_id` rather than a profile (§9.2).
    scenario: "scenario_model.Scenario | None" = None


@dataclass(frozen=True)
class ClipPlan:
    """A clip described and dressed, ready for the renderer."""

    composition: comp.Composition
    companion_path: str | None = None
    # What the scenario could not do on this clip and did anyway: an outro
    # dropped for want of room, a tag the library has nothing under. Warnings
    # rather than failures, because losing the clip to say them is a poor trade.
    notes: tuple[str, ...] = ()


def scenario_for(name: str, style: style_module.StyleSpec) -> scenario_model.Scenario:
    """The built-in scenario a profile name became."""
    return builtin.for_profile(name, style)


def wants_transcript(montage: scenario_model.Scenario) -> bool:
    """Whether this montage needs to know what is said in the clip.

    The one question worth asking across the seam before composing, because
    the answer is worth tens of seconds of Whisper on a CPU. Subtitles want
    words; so does b-roll, which is placed on the words that name it; so does
    an anchor on a spoken word. A montage with none of those should never cause
    a transcription, and this is how the caller finds out without guessing.
    """
    return scenario.FactKind.CUES in scenario.required_facts(montage)


def compose(request: ClipRequest) -> ClipPlan:
    """Everything between "this clip exists" and "hand it to ffmpeg".

    The scenario says what it needs to know about the clip, and only that is
    measured. A montage with no subtitles never asks for word timings, so
    nothing transcribes; one that keeps its pauses never runs `silencedetect`;
    one source element with a fit is a single `ffprobe` and a render. That is
    §6.1, and it is the reason this asks `required_facts` first rather than
    probing everything and discarding half of it.
    """
    montage = request.scenario or scenario_for("talking", request.style)
    needed = scenario.required_facts(montage)

    provider = scenario.CachingProvider(sources={
        scenario.FactKind.DIMENSIONS: lambda: _dimensions(request.source_path),
        scenario.FactKind.HAS_AUDIO: lambda: probe.ffprobe_has_audio(request.source_path),
        scenario.FactKind.CUTS: lambda: tuple(probe.montage_keep_segments(
            request.source_path, start_sec=request.start_sec,
            end_sec=request.end_sec, pacing=request.style.pacing,
        ) or ()),
        scenario.FactKind.ASSETS: lambda: tuple(request.library.options),
        # The words are not measured here: ASR belongs to the cutter that needs
        # it for its own work, and AUT hands them over as a fact.
        scenario.FactKind.CUES: lambda: tuple(request.speech),
    })
    facts = scenario.facts.resolve(needed, provider, base=scenario.ClipFacts(
        source_path=request.source_path,
        start_sec=request.start_sec,
        end_sec=request.end_sec,
        title=request.title_text or request.fallback_text,
        index=request.index,
        seed=request.seed,
    ))

    composition, notes = scenario.compile(montage, facts)
    for note in notes:
        log.info("clip %s: %s", request.seed, note.message)
    return ClipPlan(
        composition=composition,
        companion_path=request.library.companion_path,
        notes=tuple(note.message for note in notes),
    )


def _dimensions(source_path: str) -> tuple[int, int]:
    probed = probe.probe_media(source_path)
    return int(probed.get("width") or 0), int(probed.get("height") or 0)


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
    cache would be inspecting a composition it had never seen before, every
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
            changes["layers"] = composition.layers + chosen

    if music or sfx:
        policy = composition.style.audio
        # B-roll first, deliberately: a layer appearing is one of the moments a
        # transition sound belongs on, and it does not exist until now.
        staged = dataclasses.replace(composition, **changes) if changes else composition
        tracks: list[comp.AudioTrack] = []
        if music:
            bed = audio_planner.choose_music(staged, assets=library, policy=policy, seed=seed)
            if bed is not None:
                tracks.append(bed)
        if sfx:
            tracks.extend(
                audio_planner.choose_effects(staged, assets=library, policy=policy, seed=seed)
            )
        if tracks:
            changes["audio"] = composition.audio + tuple(tracks)

    if not changes:
        return composition
    dressed = dataclasses.replace(composition, **changes)
    log.info(
        "clip %s takes %d layer(s) and %d sound(s) from a library of %d",
        seed, len(dressed.layers), len(dressed.audio), len(library),
    )
    return dressed


def render(
    composition: comp.Composition,
    output_path: str,
    *,
    strategy: str | None = None,
    source_duration_sec: float = 0.0,
    on_progress: Progress | None = None,
) -> compiler.ClipRenderResult:
    """Turn an EDL into a file."""
    return compiler.render(
        composition, output_path,
        strategy=strategy, source_duration_sec=source_duration_sec,
    )


def cover(
    *,
    source_path: str,
    output_path: str,
    start_sec: float,
    title_text: str,
    part_text: str,
    thumbnail_path: str | None,
    width: int,
    height: int,
) -> str | None:
    """The still that goes with a clip, framed the way the clip is."""
    return probe.render_clip_cover(
        source_path=source_path, output_path=output_path, start_sec=start_sec,
        title_text=title_text, part_text=part_text, thumbnail_path=thumbnail_path,
        width=width, height=height,
    )


def cover_path_for(video_path: str) -> str:
    return probe.cover_path_for(video_path)


def excerpt(
    composition: comp.Composition, *, at_sec: float, duration_sec: float
) -> comp.Composition:
    """A few seconds out of the middle of a clip, for a preview."""
    return comp.excerpt(composition, at_sec=at_sec, duration_sec=duration_sec)


def analyse(source_path: str) -> dict[str, Any]:
    """What this file is: size, length, whether it has sound."""
    return probe.probe_media(source_path)


def has_audio(source_path: str) -> bool:
    """Whether there is any sound in this file at all."""
    return probe.ffprobe_has_audio(source_path)


def renderer_available() -> str | None:
    """The ffmpeg this service would use, or None if there is not one."""
    return probe.ffmpeg_exe()


def preview(
    output_path: str,
    *,
    spec: compiler.PreviewSpec,
    style: style_module.StyleSpec | None = None,
) -> str:
    """A few seconds rendered small, to look at a style while choosing it."""
    return compiler.render_preview(output_path, spec=spec, style=style)


PreviewSpec = compiler.PreviewSpec


def fragment_cache() -> tuple[Path, str]:
    """Where cached spine fragments live, and what they are called.

    AUT sweeps this directory on the retention schedule, which is the one place
    it has a reason to know the renderer keeps files of its own. Over HTTP this
    becomes the service sweeping its own volume.
    """
    return compiler.fragment_cache_dir(), compiler.FRAGMENT_SUFFIX


def capabilities() -> dict[str, Any]:
    """What this build of ffmpeg can actually do (§7.2).

    The probe of stage 0, served as data. The editor asks once and stops
    offering what this build cannot deliver.
    """
    return build_capabilities.capabilities().as_dict()


def subtitle_cues(
    speech: list[Any],
    *,
    timeline: list[subtitle_builder.TimelineSegment],
    fallback_text: str = "",
    style: style_module.SubtitleStyle | None = None,
) -> list[subtitle_builder.SubtitleCue]:
    """Words timed against an output timeline, for a caller that has both."""
    return subtitle_builder.make_subtitle_cues(
        speech, timeline_segments=timeline, fallback_text=fallback_text, style=style
    )
