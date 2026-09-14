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
from montage.scenario import compiler as scenario_compiler
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


def _elsewhere():
    """The remote implementation, when the service runs elsewhere (§1в).

    `None` while the montage runs in this process, which is the default and
    what every test and every single-container deployment gets. The import is
    lazy on purpose: asking the client a question must not drag an HTTP
    library into a process that will never send a request.

    Only the calls that touch files or burn CPU go through here. Building a
    scenario, trimming a composition, asking what a montage needs to know —
    those stay local, because sending arithmetic over a network to have it
    done is not a service, it is latency.
    """
    from montage.service import remote

    return remote if remote.configured() else None


def compose(request: ClipRequest) -> ClipPlan:
    """Everything between "this clip exists" and "hand it to ffmpeg"."""
    away = _elsewhere()
    return away.compose(request) if away else compose_here(request)


def compose_here(request: ClipRequest) -> ClipPlan:
    """The same, done in this process — what the service itself calls.

    The `_here` half of each travelling call exists so that the door never
    goes through the dispatcher: a service that asked itself would recurse
    until the stack gave out, and a flag saying "not this time" is a rule to
    remember rather than a shape that cannot be got wrong.

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
        scenario.FactKind.LOUDNESS: lambda: tuple(probe.loudness_curve(
            request.source_path, start_sec=request.start_sec, end_sec=request.end_sec,
        )),
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
    away = _elsewhere()
    if away is not None:
        return away.render(
            composition, output_path,
            strategy=strategy, source_duration_sec=source_duration_sec,
            on_progress=on_progress,
        )
    return render_here(
        composition, output_path,
        strategy=strategy, source_duration_sec=source_duration_sec,
    )


def render_here(
    composition: comp.Composition,
    output_path: str,
    *,
    strategy: str | None = None,
    source_duration_sec: float = 0.0,
) -> compiler.ClipRenderResult:
    """Turn an EDL into a file, in this process."""
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
    away = _elsewhere()
    if away is not None:
        return away.cover(
            source_path=source_path, output_path=output_path, start_sec=start_sec,
            title_text=title_text, part_text=part_text,
            thumbnail_path=thumbnail_path, width=width, height=height,
        )
    return cover_here(
        source_path=source_path, output_path=output_path, start_sec=start_sec,
        title_text=title_text, part_text=part_text, thumbnail_path=thumbnail_path,
        width=width, height=height,
    )


def cover_here(
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
    """The same, drawn in this process."""
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
    away = _elsewhere()
    return away.analyse(source_path) if away else analyse_here(source_path)


def analyse_here(source_path: str) -> dict[str, Any]:
    return probe.probe_media(source_path)


def has_audio(source_path: str) -> bool:
    """Whether there is any sound in this file at all."""
    away = _elsewhere()
    return away.has_audio(source_path) if away else has_audio_here(source_path)


def has_audio_here(source_path: str) -> bool:
    return probe.ffprobe_has_audio(source_path)


def renderer_available() -> str | None:
    """The ffmpeg this service would use, or None if there is not one."""
    away = _elsewhere()
    return away.renderer_available() if away else renderer_available_here()


def renderer_available_here() -> str | None:
    return probe.ffmpeg_exe()


def preview(
    composition: comp.Composition,
    output_path: str,
    *,
    spec: compiler.PreviewSpec | None = None,
) -> compiler.ClipRenderResult:
    """A few seconds of a composition rendered small, to look at while choosing.

    The window comes out of the composition rather than out of the file: what
    makes a preview cheap is that ffmpeg only ever decodes the seconds being
    looked at (§8.3). There is no style argument — the composition carries the
    style it was composed with, and a second one here would be a way to
    preview something that is not what will be rendered.
    """
    away = _elsewhere()
    if away is not None:
        return away.preview(composition, output_path, spec=spec)
    return preview_here(composition, output_path, spec=spec)


def preview_here(
    composition: comp.Composition,
    output_path: str,
    *,
    spec: compiler.PreviewSpec | None = None,
) -> compiler.ClipRenderResult:
    """The same, rendered in this process."""
    return compiler.render_preview(composition, output_path, spec=spec)


PreviewSpec = compiler.PreviewSpec


def fragment_cache() -> tuple[Path, str]:
    """Where cached spine fragments live, and what they are called.

    AUT sweeps this directory on the retention schedule, which is the one place
    it has a reason to know the renderer keeps files of its own. Over HTTP this
    becomes the service sweeping its own volume.
    """
    away = _elsewhere()
    if away is not None:
        return away.fragment_cache()
    return fragment_cache_here()


def fragment_cache_here() -> tuple[Path, str]:
    return compiler.fragment_cache_dir(), compiler.FRAGMENT_SUFFIX


def capabilities() -> dict[str, Any]:
    """What this build of ffmpeg can actually do (§7.2).

    The probe of stage 0, served as data. The editor asks once and stops
    offering what this build cannot deliver.
    """
    away = _elsewhere()
    if away is not None:
        return away.capabilities()
    return capabilities_here()


def capabilities_here() -> dict[str, Any]:
    """What this montage can do — the build's part and the compiler's.

    Two different reasons a thing is unavailable, and the editor needs both.
    A property may not animate because *this ffmpeg* cannot drive it; a slot
    may not be offered because *this compiler* does not draw it. Answering
    only the first would leave the editor offering a gradient it will drop,
    which is how a fill and a caption went on being offered for three stages
    while nothing rendered them (trap 59).

    Assembled here rather than inside the probe because this is the seam that
    knows both halves: the probe measures ffmpeg and knows nothing about
    slots, and the compiler draws slots and does not run ffmpeg.
    """
    report = build_capabilities.capabilities()
    payload = report.as_dict()
    payload["slots"] = {
        kind: all(report.by_key(key) is not None and report.by_key(key).offerable
                  for key in needs)
        for kind, needs in scenario_compiler.SLOTS_DRAWN.items()
    }
    return payload


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
