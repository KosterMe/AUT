"""Turning a `Composition` into ffmpeg invocations.

Two strategies share one vocabulary of filter chains:

**One pass** builds a single graph — every segment seeked, composed and
concatenated, inserts laid over the result, subtitles burned, audio
normalised — and encodes once. It is both the fastest and the cleanest path,
and it is the default.

**Two stages** renders each segment to its own intermediate file, joins them
with the concat demuxer without re-encoding, and spends one delivery encode on
the joined result. It costs 7-30% more time and about one VMAF point, and buys
one thing: a segment that does not have to be rendered again. Restyling a clip
(new subtitles, new inserts, new music) then costs only the final pass.

Three findings from measuring both on a real montage (40 s, four segments
spread across a 14-minute 1080p60 AV1 source, two inserts, 16 cores):

* **Seek per segment, never across them.** One input spanning the first to the
  last segment and trimming inside the graph — the shape this code replaced —
  took 30.2 s against 18.4 s, because the decoder walks every frame it is
  going to throw away.
* **Intermediates must not be MP4.** Same encoder settings, same final pass,
  only the container changed: Matroska scored VMAF 95.5 where MP4 scored 92.3.
  MP4 carries timestamp edits through `concat -c copy` that leave the delivery
  encode working off a shifted frame grid. `-avoid_negative_ts make_zero` makes
  it worse, not better.
* **Rendering segments in parallel buys nothing.** Four at once on 16 cores
  took 22.1 s against 24.0 s sequential: x264 already saturates the machine.
  So the second stage is worth choosing for its cache, never for concurrency.
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import math
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from montage.render import encoders
from montage.render import filters
from montage.render.probe import (
    ffmpeg_exe,
    ffprobe_has_audio,
    media_root,
    probe_media,
    probe_render_output,
)
from montage.config import get_settings
from montage import composition as comp
from montage import style as style_module
from montage import subtitles

log = logging.getLogger(__name__)

STRATEGY_ONE_PASS = "one_pass"
STRATEGY_TWO_STAGE = "two_stage"

# Intermediates are Matroska on purpose — see the module docstring. This is a
# constant rather than a setting because getting it wrong costs three VMAF
# points silently, which is not a trade anybody should be offered.
FRAGMENT_SUFFIX = ".mkv"

# Audio every segment is conformed to, so `concat -c copy` has something
# consistent to join and the delivery encode never resamples mid-clip.
AUDIO_RATE = 48000
AUDIO_LAYOUT = "stereo"

LAYOUT_AUTO = comp.LAYOUT_AUTO


@dataclass(frozen=True)
class ClipRenderResult:
    output_path: str
    output_duration: float
    segment_count: int
    subtitles_path: str | None
    subtitle_count: int
    silence_removed_seconds: float
    strategy: str
    fragments_reused: int
    qa: dict[str, Any]


# --------------------------------------------------------------- entry points


def render(
    composition: comp.Composition,
    output_path: str,
    *,
    strategy: str | None = None,
    source_duration_sec: float | None = None,
) -> ClipRenderResult:
    """Render a composition to `output_path`.

    `source_duration_sec` is only used to report how much was cut out; it does
    not affect the render.
    """
    if not ffmpeg_exe():
        raise RuntimeError("ffmpeg is not installed or not on PATH")
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    subtitle_path, subtitle_count = _write_subtitles(composition, output_path)
    # Only the segments are probed: an insert's audio is dropped, and music and
    # effects are known to have some — probing them would be two subprocesses
    # per clip spent confirming what putting them there already asserted.
    audio_by_source = {
        segment.source_path: ffprobe_has_audio(segment.source_path)
        for segment in composition.spine
    }
    has_audio = (
        any(audio_by_source.get(s.source_path, False) for s in composition.spine)
        or composition.has_own_audio
    )

    chosen = strategy or choose_strategy(composition)
    if chosen == STRATEGY_TWO_STAGE:
        reused = _render_two_stage(
            composition, output_path,
            subtitle_path=subtitle_path, audio_by_source=audio_by_source, has_audio=has_audio,
        )
    else:
        _render_one_pass(
            composition, output_path,
            subtitle_path=subtitle_path, audio_by_source=audio_by_source, has_audio=has_audio,
        )
        reused = 0

    duration = composition.duration_sec
    removed = max(0.0, (source_duration_sec or duration) - duration)
    qa = probe_render_output(
        output_path,
        expected_width=composition.canvas.width,
        expected_height=composition.canvas.height,
        expected_duration=duration,
        expected_has_audio=has_audio,
    )
    log.info(
        "rendered %s via %s: %d segment(s), %d insert(s), %d fragment(s) reused",
        os.path.basename(output_path), chosen, len(composition.spine),
        len(composition.layers), reused,
    )
    return ClipRenderResult(
        output_path=output_path,
        output_duration=round(duration, 3),
        segment_count=len(composition.spine),
        subtitles_path=subtitle_path,
        subtitle_count=subtitle_count,
        silence_removed_seconds=round(removed, 3),
        strategy=chosen,
        fragments_reused=reused,
        qa=qa,
    )


@dataclass(frozen=True)
class PreviewSpec:
    """A few seconds of a clip, small and quick, for looking at a style.

    The whole point is the length of the loop. A full render is twenty seconds
    of ffmpeg and produces a file nobody keeps; four seconds at half size is
    about one, which is the difference between tuning a look and guessing at
    one. Quality is deliberately lower — this is for judging framing, type and
    pacing, never for judging compression.
    """

    at_sec: float = 0.0
    duration_sec: float = 4.0
    scale: float = 0.5
    crf: int = 30

    def __post_init__(self) -> None:
        if self.duration_sec <= 0:
            raise ValueError("a preview needs a positive duration")


def render_preview(
    composition: comp.Composition, output_path: str, *, spec: PreviewSpec | None = None
) -> ClipRenderResult:
    """Render one window of a composition at reduced size.

    Always one pass, and never through the fragment cache: a preview is a
    throwaway at a size nothing else uses, and filling the cache with half-size
    fragments would only make the real render miss.
    """
    window = spec or PreviewSpec()
    excerpt = comp.scaled(
        comp.excerpt(composition, at_sec=window.at_sec, duration_sec=window.duration_sec),
        window.scale,
    )
    excerpt = dataclasses.replace(
        excerpt,
        style=dataclasses.replace(
            excerpt.style, delivery=excerpt.style.delivery.merged({"crf": window.crf})
        ),
    )
    return render(excerpt, output_path, strategy=STRATEGY_ONE_PASS)


def choose_strategy(composition: comp.Composition) -> str:
    """Which strategy to render this composition with.

    One pass wins on both time and quality, so it is the default and the
    exceptions have to earn themselves: a graph large enough to be hard to
    debug, or a cache warm enough to pay for the extra encode. Operators
    iterating on look — where every clip is rendered repeatedly — can force
    two stages with `AUTOCLIPS_RENDER_STRATEGY`.
    """
    settings = get_settings().render
    # The clip's own style wins: a job being iterated on can ask for the
    # cacheable path without changing what every other job does.
    configured = (composition.style.delivery.strategy or settings.strategy or "auto")
    configured = configured.strip().lower()
    if configured in (STRATEGY_ONE_PASS, STRATEGY_TWO_STAGE):
        return configured

    if len(composition.spine) > settings.one_pass_max_segments:
        return STRATEGY_TWO_STAGE
    if len(composition.layers) > settings.one_pass_max_layers:
        return STRATEGY_TWO_STAGE
    if len(composition.framings) > 1:
        return STRATEGY_TWO_STAGE
    if _cached_share(composition) >= 0.5:
        return STRATEGY_TWO_STAGE
    return STRATEGY_ONE_PASS


def frame_for_source(
    source_path: str,
    canvas: comp.Canvas,
    *,
    requested: str = LAYOUT_AUTO,
    tolerance: float = 0.15,
) -> tuple[comp.Frame, bool]:
    """How this source should fill the canvas: a rectangle and a backdrop.

    Only `auto` is decided here; a style that asked for a particular framing
    gets it. The decision is about shape and nothing else: a source that is
    already about as tall and narrow as the canvas is cropped to fill it,
    because giving a vertical video a blurred backdrop made of itself is a
    frame of wasted screen. Anything wider keeps the backdrop.

    The named layouts are still the vocabulary a style speaks — they leave when
    profiles become scenarios (§9.2) — but nothing past this point knows them.
    """
    if requested and requested != LAYOUT_AUTO:
        return comp.frame_for_layout(requested), requested == comp.LAYOUT_BLUR

    probe = probe_media(source_path)
    width, height = probe.get("width"), probe.get("height")
    if not width or not height:
        # Cropping to a shape nobody measured is the worse guess.
        return comp.CONTAINED, True

    source_aspect = float(width) / float(height)
    canvas_aspect = canvas.width / canvas.height
    if source_aspect <= canvas_aspect * (1.0 + tolerance):
        return comp.FULL_FRAME, False
    return comp.CONTAINED, True


def _unsplit(layout: str) -> str:
    """A split screen that lost its bottom half falls back to deciding on shape."""
    return LAYOUT_AUTO if layout == comp.LAYOUT_SPLIT else layout


def plan_vertical_clip(
    input_path: str,
    *,
    start_sec: float,
    end_sec: float,
    style: style_module.StyleSpec | None = None,
    companion_path: str | None = None,
    transcript_segments: list[Any] | None = None,
    fallback_subtitle_text: str | None = None,
    title_text: str | None = None,
) -> comp.Composition:
    """Describe one slice of one file, without rendering anything.

    **No production path calls this any more.** The live path is
    `compile(scenario, facts)`, and this is what it replaced: one function that
    probed the file and decided everything about the clip from a style. It
    stays because the migration test's claim is about it — that the four
    built-in scenarios produce the same EDL the pipeline produced before them —
    and a claim whose other side has been deleted is a claim nobody can check
    (§9.2).

    That test *calls* it, with its two probes answered from the same
    `ClipFacts` the scenario side uses. Until it did, this function was not run
    by anything in the repository, and a reference implementation nobody runs
    is documentation that rots quietly (trap 57).

    What it shows, and the reason it was a dead end: the probes are wired in.
    Silence removal happens here, the frame is chosen here, and a caller who
    wanted a montage without paying for a `silencedetect` pass had nowhere to
    say so. `required_facts` exists because of this function.

    The style arrives resolved. Every decision this function makes — where the
    cuts go, how the frame is filled, what the type looks like — reads from it
    and from nothing else, which is what makes the resulting composition a
    complete description of the clip rather than half of one.
    """
    from montage.render.probe import montage_keep_segments

    look = style or style_module.StyleSpec.from_settings()
    canvas = comp.canvas_for(look)

    keep_segments = None
    if look.pacing.remove_silence:
        keep_segments = montage_keep_segments(
            input_path, start_sec=start_sec, end_sec=end_sec, pacing=look.pacing
        )

    wants_split = look.framing.layout == comp.LAYOUT_SPLIT
    if wants_split and not companion_path:
        # Asked for a split screen with nothing to put in the bottom half. The
        # clip is still worth making, so it falls back rather than failing.
        log.warning("no companion source for a split screen; falling back to a blurred backdrop")
        wants_split = False

    frame, backdrop = frame_for_source(
        input_path, canvas,
        requested=comp.LAYOUT_SPLIT if wants_split else _unsplit(look.framing.layout),
        tolerance=look.framing.fill_tolerance,
    )
    draft = comp.single_source(
        input_path, start_sec=start_sec, end_sec=end_sec, keep_segments=keep_segments,
        canvas=canvas, style=look, frame=frame, backdrop=backdrop,
    )
    if wants_split and companion_path:
        # One layer across the clip rather than a second source on every
        # segment: the bottom half runs continuously and always did.
        draft = dataclasses.replace(draft, layers=draft.layers + (comp.Layer(
            source_path=companion_path,
            at_sec=0.0,
            duration_sec=draft.duration_sec,
            frame=comp.BOTTOM_HALF,
            z=-1,
        ),))
    cues: list[subtitles.SubtitleCue] = []
    if look.subtitles.enabled:
        cues = subtitles.make_subtitle_cues(
            transcript_segments or [],
            timeline_segments=draft.timeline(),
            fallback_text=fallback_subtitle_text,
            style=look.subtitles,
        )
    return dataclasses.replace(
        draft,
        subtitles=comp.SubtitleSpec(cues=tuple(cues), title_text=title_text),
    )


# ------------------------------------------------------------------ strategies


def one_pass_args(
    composition: comp.Composition,
    output_path: str,
    *,
    subtitle_path: str | None,
    audio_by_source: dict[str, bool],
    has_audio: bool,
    encoder: str,
) -> list[str]:
    """The whole composition as one ffmpeg invocation.

    Separate from running it so the graph can be asserted on in tests: a
    filter graph is the compiler's real output, and comparing strings is far
    cheaper than comparing frames.
    """
    inputs: list[str] = []
    parts: list[str] = []
    video_labels: list[str] = []
    audio_labels: list[str] = []

    for index, segment in enumerate(composition.spine):
        video_input = _add_input(inputs, segment.source_path,
                                 start=segment.source_start_sec, duration=segment.duration_sec)
        chain, video_label = _segment_video_chain(
            index, composition.canvas, segment,
            video_in=f"[{video_input}:v]",
            framing=composition.style.framing,
        )
        parts.extend(chain)
        video_labels.append(video_label)

        if has_audio:
            audio_in = f"[{video_input}:a]" if audio_by_source.get(segment.source_path) else None
            audio_chain, audio_label = _segment_audio_chain(index, segment, audio_in=audio_in)
            parts.extend(audio_chain)
            audio_labels.append(audio_label)

    video_label, audio_label = _concat(parts, video_labels, audio_labels if has_audio else [])

    layer_chains, video_label = _layer_chains(composition, inputs, video_in=video_label)
    parts.extend(layer_chains)
    parts.append(
        _look_chain(
            video_in=video_label, subtitle_path=subtitle_path, grade=composition.style.grade
        )
    )
    if has_audio:
        parts.extend(_audio_chains(composition, inputs, audio_in=audio_label))

    return [
        ffmpeg_exe(), "-y", *inputs, "-filter_complex", ";".join(parts),
        *_delivery_tail(output_path, crf=composition.crf, encoder=encoder,
                        has_audio=has_audio, shortest=True),
    ]


def final_pass_args(
    composition: comp.Composition,
    joined_path: str,
    output_path: str,
    *,
    subtitle_path: str | None,
    has_audio: bool,
    encoder: str,
) -> list[str]:
    """The second stage: inserts, look and audio over the joined segments."""
    inputs = ["-i", joined_path]
    parts: list[str] = []
    layer_chains, video_label = _layer_chains(composition, inputs, video_in="0:v")
    parts.extend(layer_chains)
    parts.append(
        _look_chain(
            video_in=video_label, subtitle_path=subtitle_path, grade=composition.style.grade
        )
    )
    if has_audio:
        parts.extend(_audio_chains(composition, inputs, audio_in="0:a"))

    return [
        ffmpeg_exe(), "-y", *inputs, "-filter_complex", ";".join(parts),
        *_delivery_tail(output_path, crf=composition.crf, encoder=encoder,
                        has_audio=has_audio, shortest=False),
    ]


def _delivery_tail(
    output_path: str, *, crf: int, encoder: str, has_audio: bool, shortest: bool
) -> list[str]:
    args = ["-map", "[v]"]
    args.extend(["-map", "[a]"] if has_audio else ["-an"])
    args.extend(encoders.encoder_args(encoder, crf=crf))
    if has_audio:
        args.extend(["-c:a", "aac", "-b:a", "160k"])
    args.extend(["-movflags", "+faststart"])
    if shortest:
        args.append("-shortest")
    args.append(output_path)
    return args


def _render_one_pass(
    composition: comp.Composition,
    output_path: str,
    *,
    subtitle_path: str | None,
    audio_by_source: dict[str, bool],
    has_audio: bool,
) -> None:
    _run_with_encoder_fallback(
        lambda encoder: one_pass_args(
            composition, output_path, subtitle_path=subtitle_path,
            audio_by_source=audio_by_source, has_audio=has_audio, encoder=encoder,
        ),
        label="one-pass render",
    )


def _render_two_stage(
    composition: comp.Composition,
    output_path: str,
    *,
    subtitle_path: str | None,
    audio_by_source: dict[str, bool],
    has_audio: bool,
) -> int:
    """Render each segment, join them, then spend one encode on the result.

    Returns how many segments came out of the cache.
    """
    settings = get_settings().render
    fragments: list[str] = []
    reused = 0
    for index, segment in enumerate(composition.spine):
        path = fragment_path(composition.canvas, segment, framing=composition.style.framing)
        if settings.fragment_cache and os.path.exists(path) and os.path.getsize(path) > 2048:
            reused += 1
            os.utime(path, None)  # keep the cache sweep honest about what is in use
        else:
            _render_fragment(
                composition, segment, index, path,
                has_audio=has_audio and bool(audio_by_source.get(segment.source_path)),
                keep_audio=has_audio,
            )
        fragments.append(path)

    joined = _concat_fragments(fragments, output_path)
    try:
        _run_with_encoder_fallback(
            lambda encoder: final_pass_args(
                composition, joined, output_path,
                subtitle_path=subtitle_path, has_audio=has_audio, encoder=encoder,
            ),
            label="final pass",
        )
    finally:
        _remove_quietly(joined)
        if not settings.fragment_cache:
            # With the cache off these were scratch files, not a cache. Leaving
            # them would fill the disk with fragments nothing will ever read.
            for path in fragments:
                _remove_quietly(path)
    return reused


def fragment_args(
    composition: comp.Composition,
    segment: comp.Segment,
    index: int,
    output_path: str,
    *,
    has_audio: bool,
    keep_audio: bool,
    encoder: str | None = None,
) -> list[str]:
    """One segment, composed into the canvas and nothing else.

    Deliberately free of subtitles, inserts and grading: those belong to the
    final pass, so changing any of them leaves every cached fragment valid.
    """
    settings = get_settings().render
    inputs: list[str] = []
    video_input = _add_input(inputs, segment.source_path,
                             start=segment.source_start_sec, duration=segment.duration_sec)

    parts, video_label = _segment_video_chain(
        index, composition.canvas, segment,
        video_in=f"[{video_input}:v]",
        framing=composition.style.framing,
    )
    parts.append(f"[{video_label}]format=yuv420p,setsar=1[v]")
    if keep_audio:
        audio_chain, audio_label = _segment_audio_chain(
            index, segment, audio_in=f"[{video_input}:a]" if has_audio else None
        )
        parts.extend(audio_chain)
        parts.append(f"[{audio_label}]anull[a]")

    args = [ffmpeg_exe(), "-y", *inputs, "-filter_complex", ";".join(parts), "-map", "[v]"]
    args.extend(["-map", "[a]"] if keep_audio else ["-an"])
    args.extend(
        encoders.encoder_args(encoder or settings.fragment_encoder, crf=settings.fragment_crf)
    )
    if keep_audio:
        args.extend(["-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_RATE)])
    args.append(output_path)
    return args


def _render_fragment(
    composition: comp.Composition,
    segment: comp.Segment,
    index: int,
    output_path: str,
    *,
    has_audio: bool,
    keep_audio: bool,
) -> None:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    # Written under a scratch name and moved into place, so a worker killed
    # mid-encode cannot leave a truncated fragment that later reads as a hit.
    temporary = output_path + ".partial" + FRAGMENT_SUFFIX
    args = fragment_args(
        composition, segment, index, temporary, has_audio=has_audio, keep_audio=keep_audio
    )
    _run_ffmpeg(args)
    os.replace(temporary, output_path)


# ------------------------------------------------------------- filter vocabulary


def _segment_video_chain(
    index: int,
    canvas: comp.Canvas,
    segment: comp.Segment,
    *,
    video_in: str,
    framing: style_module.FramingStyle | None = None,
) -> tuple[list[str], str]:
    """Compose one segment into the canvas. Returns (chain parts, out label).

    Three branches became one rectangle and one flag. What used to be `fill`
    is a frame covering the canvas; `blur` is a frame contained inside it with
    a blurred copy of itself behind; `split` is a frame filling half of it,
    with the other half left for a layer. Combinations nobody could ask for
    before — a source letterboxed on black, a source in the upper third —
    follow from the same two fields rather than from a fourth branch.
    """
    prefix = f"s{index}"
    out = f"v{index}"
    look = framing or style_module.FramingStyle.from_settings()
    normalise = f"fps={canvas.fps},setpts=PTS-STARTPTS"
    left, top, width, height = segment.frame.box(canvas)
    height = height or canvas.height

    if segment.backdrop:
        # The backdrop is made from this segment and nothing else, which is
        # why it lives here rather than as a layer: there is no other source
        # to take it from. Shrinking before the blur costs the divisor squared
        # less for the same look, since a heavy blur discards the detail the
        # downscale removed anyway.
        parts = [
            f"{video_in}{normalise},split=2[{prefix}bgsrc][{prefix}fgsrc]",
            filters.background(canvas.width, canvas.height, src=f"[{prefix}bgsrc]",
                               out=f"{prefix}bg", divisor=look.blur_divisor,
                               radius=look.blur_radius),
            filters.foreground(canvas.width, canvas.height, src=f"[{prefix}fgsrc]",
                               out=f"{prefix}fg", zoom=look.zoom),
            f"[{prefix}bg][{prefix}fg]overlay=(W-w)/2:(H-h)/2[{out}]",
        ]
        return parts, out

    if (left, top, width, height) == (0, 0, canvas.width, canvas.height):
        return (
            [filters.fill(canvas.width, canvas.height, src=f"{video_in}{normalise},", out=out)],
            out,
        )

    # A frame smaller than the canvas: fill the box, then place it. What the
    # box does not cover stays black, and a layer is free to sit there — which
    # is exactly what the bottom half of a split screen now is.
    return (
        [
            f"{video_in}{normalise},"
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},"
            f"pad={canvas.width}:{canvas.height}:{left}:{top}:black[{out}]"
        ],
        out,
    )


def _segment_audio_chain(
    index: int, segment: comp.Segment, *, audio_in: str | None
) -> tuple[list[str], str]:
    """One segment's audio, conformed so segments can be concatenated.

    A source without an audio stream contributes silence rather than being
    skipped: dropping it would desynchronise everything after it.
    """
    out = f"a{index}"
    conform = _conform()
    if audio_in:
        return ([f"{audio_in}asetpts=PTS-STARTPTS,{conform}[{out}]"], out)
    return (
        [
            f"anullsrc=r={AUDIO_RATE}:cl={AUDIO_LAYOUT},"
            f"atrim=duration={filters.flt(segment.duration_sec)},asetpts=PTS-STARTPTS[{out}]"
        ],
        out,
    )


def _concat(
    parts: list[str], video_labels: list[str], audio_labels: list[str]
) -> tuple[str, str]:
    """Join the composed segments. A single segment needs no concat at all."""
    if len(video_labels) == 1:
        return video_labels[0], (audio_labels[0] if audio_labels else "")

    has_audio = bool(audio_labels)
    pairs = []
    for index, video in enumerate(video_labels):
        pairs.append(f"[{video}]")
        if has_audio:
            pairs.append(f"[{audio_labels[index]}]")
    parts.append(
        "".join(pairs)
        + f"concat=n={len(video_labels)}:v=1:a={1 if has_audio else 0}"
        + ("[vcat][acat]" if has_audio else "[vcat]")
    )
    return "vcat", ("acat" if has_audio else "")


def _layer_chains(
    composition: comp.Composition, inputs: list[str], *, video_in: str
) -> tuple[list[str], str]:
    """Lay every layer over the assembled spine, bottom of the stack first.

    `overlay` has no z of its own — it composites one picture onto another, in
    the order it is applied — so the stack order *is* the order of these
    filters, and `Composition.stack` is the single place that order is decided.

    Each layer is padded at the front with `tpad` so overlay always has a frame
    available at the moment it becomes visible; `enable` does the actual
    switching. Layer audio is dropped: the clip's own soundtrack keeps running
    underneath, which is what keeps subtitles aligned.

    Where the old code branched on `broll_full` versus `broll_pip`, this reads
    the layer's frame. The two kinds were a rectangle covering the canvas and a
    rectangle in the corner, and a rectangle is something you can write down.
    """
    parts: list[str] = []
    current = video_in
    canvas = composition.canvas
    for order, layer in enumerate(composition.stack):
        index = _add_input(inputs, layer.source_path, start=layer.source_start_sec,
                           duration=layer.duration_sec, still=layer.still)
        tag = f"ins{order}"
        fit, position = _layer_geometry(layer.frame, canvas, since=layer.at_sec)
        veil, head = _veil(layer, canvas, order=order, source=f"[{index}:v]")
        parts.extend(veil)
        parts.append(
            f"{head}{fit},setpts=PTS-STARTPTS,"
            f"tpad=start_duration={filters.flt(layer.at_sec)}:start_mode=add:color=black[{tag}]"
        )
        out = f"vins{order}"
        parts.append(
            f"[{current}][{tag}]overlay={position}:eof_action=pass:"
            f"enable='between(t,{filters.flt(layer.at_sec)},{filters.flt(layer.end_sec)})'[{out}]"
        )
        current = out
    return parts, current


def _veil(
    layer: comp.Layer, canvas: comp.Canvas, *, order: int, source: str
) -> tuple[list[str], str]:
    """Hold part of a layer back, and the start of its chain either way.

    Two constructions, both measured on this build (§7.2) and both of which
    *multiply* the layer's own alpha rather than replacing it — a sticker with
    a hole in it has to keep the hole:

    * a constant is `colorchannelmixer=aa=…`, one filter and no second input;
    * a curve is drawn by `geq` on a mask sixteen pixels square, stretched
      over the layer by `scale2ref`, multiplied into the alpha the layer
      already has, and merged back.

    The mask is small because `geq` is priced per pixel of its own input. Over
    a whole canvas it costs ×23, which is what stage 6 recorded against the
    property and why opacity did not animate then; over 16×16 and a stretch it
    costs ×3 (trap 50).

    The curve is shifted onto the layer's own clock for the same reason `fit`
    is: everything up to `tpad` runs before the layer is moved into place, so
    its second zero is the layer's first frame and not the clip's.
    """
    frame = layer.frame
    fps = canvas.fps
    if not frame.sheer:
        return [], f"{source}fps={fps},"
    if not frame.fades:
        held = filters.flt(frame.opacity)
        return [], f"{source}fps={fps},format=rgba,colorchannelmixer=aa={held},"

    assert frame.motion is not None  # `frame.fades` is what got us here
    curve = filters.polyline(
        [
            (round(at - layer.at_sec, 3), round(255.0 * max(0.0, min(1.0, value)), 3))
            for at, value in frame.motion.opacity
        ],
        clock="T",
    )
    mask, own, merged = f"m{order}", f"own{order}", f"veil{order}"
    return [
        # No duration on the mask and `shortest` on the merge: the layer
        # decides how long it is, and a mask cut to a length worked out here
        # would either run out early or hold the graph open past the end.
        f"color=c=black:s=16x16:r={fps},format=gray,geq=lum='{curve}'[{mask}]",
        f"{source}fps={fps},format=rgba,split[{merged}a][{merged}b]",
        f"[{merged}a]alphaextract[{own}]",
        f"[{mask}][{own}]scale2ref[{mask}s][{own}s]",
        f"[{own}s][{mask}s]blend=all_mode=multiply:shortest=1[{merged}]",
    ], f"[{merged}b][{merged}]alphamerge,"


def _layer_geometry(
    frame: comp.Frame, canvas: comp.Canvas, *, since: float = 0.0
) -> tuple[str, str]:
    """How to scale a layer and where to put it, from its frame.

    A frame covering the canvas is scaled up and cropped — there is nothing
    beside it to position against. Anything smaller keeps its aspect ratio
    (`-2` lets ffmpeg choose the height, as the picture-in-picture always did)
    and is placed by its top-left corner, which is the one number `overlay`
    takes.

    A frame that moves takes a second road, and only then: a composition
    without animation has to compile to the string it compiled to before
    animation existed, or every scenario starts paying for a feature it does
    not use (§4.3).
    """
    if frame.fills_canvas:
        return (
            f"scale={canvas.width}:{canvas.height}:force_original_aspect_ratio=increase,"
            f"crop={canvas.width}:{canvas.height}",
            "0:0",
        )
    left, top, width, height = frame.box(canvas)
    if not height:
        # No height asked for, so the aspect ratio decides it — `-2` keeps the
        # dimension even, which yuv420p requires.
        fit = f"scale={width}:-2"
    elif frame.fit == "cover":
        # A box with both dimensions and something that has to fill it: scale
        # past and crop, rather than stretch a wide source into a tall hole.
        fit = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )
    else:
        fit = f"scale={width}:{height}"

    if not frame.moves:
        return fit, f"{left}:{top}"
    return _moving_geometry(
        frame, canvas, fit=fit, width=width, height=height, left=left, top=top,
        since=since,
    )


def _moving_geometry(
    frame: comp.Frame,
    canvas: comp.Canvas,
    *,
    fit: str,
    width: int,
    height: int,
    left: int,
    top: int,
    since: float = 0.0,
) -> tuple[str, str]:
    """The chain and the position for a frame that moves.

    Two clocks, and the difference between them is `since`. `overlay` runs
    after the layer has been padded into place, so its `t` is the clip's;
    `scale` and `rotate` run before that, where the layer's own first frame is
    zero. A curve is written against the clip (§4.3), so the chain gets it
    shifted and `overlay` gets it as it stands. Before this was noticed, a
    layer that started at 0:03 grew three seconds early (trap 51).

    Three constructions, each one measured on this build rather than read out
    of the documentation (§7.2):

    * `scale=w='…t…':eval=frame` resizes on every frame. `eval` defaults to
      `init`, which is the whole difference between a layer that grows and a
      layer that is simply the wrong size.
    * `rotate=a='…t…'` turns it — but `ow`/`oh` are evaluated once, at a point
      where `t` does not exist yet, so the box is cut for the widest angle the
      curve reaches and the picture turns inside it.
    * `overlay=x='…':y='…'` places it, and where the box changes size it is
      placed by `w`/`h` — the layer's *current* dimensions — rather than by
      numbers worked out in advance, which would be the size it used to be.
    """
    motion = frame.motion
    assert motion is not None  # `frame.moves` is what got us here

    chain = [fit]
    if motion.width or motion.height:
        chain = [_moving_scale(motion, canvas, width=width, height=height, since=since)]
    if motion.rotate:
        chain.append(_moving_rotate(motion, since=since))

    return ",".join(chain), _moving_position(
        motion, canvas, width=width, height=height, left=left, top=top,
        sized_at_runtime=motion.resizes,
    )


def _moving_scale(
    motion: comp.Motion, canvas: comp.Canvas, *, width: int, height: int,
    since: float = 0.0,
) -> str:
    """`scale` with an expression per side, for the sides that move."""
    across = (
        filters.polyline([
            (round(at - since, 3), round(canvas.width * value / 100.0, 3))
            for at, value in motion.width
        ])
        if motion.width else str(width)
    )
    down = (
        filters.polyline([
            (round(at - since, 3), round(canvas.height * value / 100.0, 3))
            for at, value in motion.height
        ])
        if motion.height
        # A layer that left its height to the aspect ratio keeps doing so
        # while it grows: `-2` is not a size, it is "work it out and keep it
        # even".
        else (str(height) if height else "-2")
    )
    return f"scale=w='{across}':h='{down}':eval=frame"


def _moving_rotate(motion: comp.Motion, *, since: float = 0.0) -> str:
    """`rotate` with an angle that moves, in a box cut for the widest one.

    Degrees on the way in, because that is what somebody types; radians on the
    way out, because that is what ffmpeg reads.
    """
    radians = filters.polyline([
        (round(at - since, 3), round(value * math.pi / 180.0, 6))
        for at, value in motion.rotate
    ])
    widest = round(max(abs(value) for _, value in motion.rotate) * math.pi / 180.0, 6)
    # `format=rgba` first: the corners the turn leaves empty have to be
    # transparent, or the layer arrives as a black diamond.
    return (
        f"format=rgba,rotate=a='{radians}'"
        f":ow=rotw({filters.flt(widest)}):oh=roth({filters.flt(widest)}):c=none"
    )


def _moving_position(
    motion: comp.Motion,
    canvas: comp.Canvas,
    *,
    width: int,
    height: int,
    left: int,
    top: int,
    sized_at_runtime: bool,
) -> str:
    """`overlay`'s x and y as expressions in `t`.

    The curve is in per cent of the canvas, because that is what survives a
    change of canvas; `overlay` wants pixels of the top-left corner. Where the
    box keeps its size that conversion is arithmetic done here, once; where it
    does not, it is written as an expression over `w`/`h` and ffmpeg does it
    per frame.
    """
    def horizontal(value: float) -> float:
        return round(canvas.width * value / 100.0 - width / 2.0, 3)

    def vertical(value: float) -> float:
        # The same asymmetry `box` has: a layer whose height follows from its
        # width is positioned by the number itself, because there is no height
        # to take half of yet.
        return round(
            canvas.height * value / 100.0 - (height / 2.0 if height else 0.0), 3
        )

    if not sized_at_runtime:
        x = (
            filters.polyline([(at, horizontal(value)) for at, value in motion.x])
            if motion.x else str(left)
        )
        y = (
            filters.polyline([(at, vertical(value)) for at, value in motion.y])
            if motion.y else str(top)
        )
        return f"x='{x}':y='{y}'"

    centre_x = (
        filters.polyline([(at, value) for at, value in motion.x])
        if motion.x else filters.flt(100.0 * (left + width / 2.0) / canvas.width)
    )
    # Whether `y` is the middle of the box or its top edge — the asymmetry
    # `box` has, kept rather than quietly fixed while something else is being
    # added. A height that moves is a height, so it centres.
    centred = bool(height) or bool(motion.height)
    centre_y = (
        filters.polyline([(at, value) for at, value in motion.y])
        if motion.y
        else filters.flt(
            100.0 * (top + (height / 2.0 if height and centred else 0.0)) / canvas.height
        )
    )
    return (
        f"x='({centre_x})*{canvas.width}/100-w/2'"
        f":y='({centre_y})*{canvas.height}/100{'-h/2' if centred else ''}'"
    )


def _look_chain(
    *, video_in: str, subtitle_path: str | None, grade: style_module.GradeStyle | None = None
) -> str:
    """Grade, sharpen and burn the overlay — everything that is style.

    Kept out of the segment chain on purpose: a cached fragment stays valid
    when the look changes. Measured to cost nothing either way.
    """
    look = grade or style_module.GradeStyle.from_settings()
    chain = (
        f"[{video_in}]eq=contrast={look.contrast:.2f}:saturation={look.saturation:.2f}"
    )
    if look.sharpen:
        chain += f",unsharp=5:5:{look.sharpen_luma:.2f}:3:3:{look.sharpen_chroma:.2f}"
    chain += ",format=yuv420p,setsar=1"
    if subtitle_path:
        chain += (
            f",subtitles=filename='{filters.path(subtitle_path)}'{filters.fontsdir_arg()}"
        )
    return chain + "[v]"


def _audio_chains(
    composition: comp.Composition, inputs: list[str], *, audio_in: str
) -> list[str]:
    """The music bed, the effects, and the delivery normalisation.

    Everything here happens after the segments are joined, which is what keeps
    a cached fragment valid when the music changes — and what keeps the mix
    from being normalised once per segment and again at the end.

    The bed is compressed against the voice rather than set to a fixed low
    level: a level quiet enough under speech is inaudible in a pause, and one
    audible in a pause fights the speech.
    """
    duration = composition.duration_sec
    parts: list[str] = []
    voice = audio_in
    extra: list[str] = []

    for order, bed in enumerate(composition.beds):
        label = "bed" if order == 0 else f"bed{order}"
        index = _add_input(
            inputs, bed.source_path,
            start=bed.source_start_sec, duration=duration, loop=True,
        )
        fade_out_start = max(0.0, duration - bed.fade_out_sec)
        parts.append(
            f"[{index}:a]{_conform()},volume={bed.gain_db:.2f}dB,"
            f"afade=t=in:st=0:d={filters.flt(bed.fade_in_sec)},"
            f"afade=t=out:st={filters.flt(fade_out_start)}:d={filters.flt(bed.fade_out_sec)}"
            f"[{label}]"
        )
        if not bed.ducks:
            extra.append(label)
            continue
        # The voice is needed twice: once in the mix, once as the trigger that
        # tells the compressor when to pull the bed down. Split once, however
        # many beds ask to be ducked.
        if voice == audio_in:
            parts.append(f"[{voice}]asplit=2[voicemix][voicekey]")
            voice = "voicemix"
        ducked = "ducked" if order == 0 else f"ducked{order}"
        parts.append(
            f"[{label}][voicekey]sidechaincompress="
            f"threshold={bed.duck_threshold:.4f}:ratio={bed.duck_ratio:.2f}:"
            f"attack={bed.duck_attack_ms:.2f}:release={bed.duck_release_ms:.2f}:"
            f"makeup=1[{ducked}]"
        )
        extra.append(ducked)

    for order, effect in enumerate(sorted(composition.stingers, key=lambda e: e.at_sec)):
        index = _add_input(inputs, effect.source_path, start=0.0, duration=effect.duration_sec)
        label = f"sfx{order}"
        delay_ms = int(round(effect.at_sec * 1000))
        # adelay wants one value per channel; the mix is stereo throughout.
        # The tail fade is what stops a truncated effect ending in a click.
        parts.append(
            f"[{index}:a]{_conform()},volume={effect.gain_db:.2f}dB,"
            f"afade=t=out:st={filters.flt(max(0.0, effect.duration_sec - 0.05))}:d=0.05,"
            f"adelay={delay_ms}|{delay_ms}[{label}]"
        )
        extra.append(label)

    if extra:
        # normalize=0 because amix otherwise divides every input by their
        # number, which would drop the voice by 6 dB for the crime of having
        # music under it. loudnorm below sorts the overall level out.
        parts.append(
            f"[{voice}]" + "".join(f"[{label}]" for label in extra)
            + f"amix=inputs={1 + len(extra)}:normalize=0:duration=first:"
            "dropout_transition=0[amixed]"
        )
        voice = "amixed"

    fade_start = max(0.0, duration - 0.12)
    parts.append(
        f"[{voice}]loudnorm=I=-14:TP=-1.5:LRA=11,"
        f"afade=t=in:st=0:d=0.08,afade=t=out:st={filters.flt(fade_start)}:d=0.12[a]"
    )
    return parts


def _conform() -> str:
    return f"aformat=sample_rates={AUDIO_RATE}:channel_layouts={AUDIO_LAYOUT}"


# ------------------------------------------------------------------ fragments


def fragment_cache_dir() -> str:
    directory = os.path.join(media_root(), "fragments")
    os.makedirs(directory, exist_ok=True)
    return directory


def fragment_path(
    canvas: comp.Canvas,
    segment: comp.Segment,
    framing: style_module.FramingStyle | None = None,
) -> str:
    """Where a rendered segment lives, named by everything that shaped it.

    The key covers the source's identity as well as its path: replacing a file
    in place would otherwise serve the old picture forever. It also covers the
    framing, which is the only part of the style a fragment can see — the rest
    is applied after the join, which is exactly why a restyle can reuse them.
    """
    settings = get_settings().render
    frame = framing or style_module.FramingStyle.from_settings()
    material = "|".join(
        str(part)
        for part in (
            comp.VERSION,
            canvas.width, canvas.height, canvas.fps,
            segment.frame, segment.backdrop,
            _source_identity(segment.source_path),
            f"{segment.source_start_sec:.3f}", f"{segment.source_end_sec:.3f}",
            frame.zoom, frame.blur_divisor, frame.blur_radius,
            settings.fragment_encoder, settings.fragment_crf,
        )
    )
    digest = hashlib.sha1(material.encode("utf-8")).hexdigest()[:20]
    return os.path.join(fragment_cache_dir(), f"seg-{digest}{FRAGMENT_SUFFIX}")


def _source_identity(path: str | None) -> str:
    if not path:
        return ""
    try:
        stat = os.stat(path)
        return f"{path}|{stat.st_size}|{int(stat.st_mtime)}"
    except OSError:
        return path


def _cached_share(composition: comp.Composition) -> float:
    """Fraction of the output already sitting in the fragment cache."""
    if not get_settings().render.fragment_cache:
        return 0.0
    total = composition.duration_sec
    if total <= 0:
        return 0.0
    cached = sum(
        segment.duration_sec
        for segment in composition.spine
        if os.path.exists(
            fragment_path(composition.canvas, segment, framing=composition.style.framing)
        )
    )
    return cached / total


def _concat_fragments(fragments: list[str], output_path: str) -> str:
    listing = os.path.join(media_root(), "tmp", f"{Path(output_path).stem}.concat.txt")
    os.makedirs(os.path.dirname(listing), exist_ok=True)
    with open(listing, "w", encoding="utf-8") as handle:
        for path in fragments:
            handle.write(f"file '{Path(path).as_posix()}'\n")

    joined = os.path.join(media_root(), "tmp", f"{Path(output_path).stem}.joined{FRAGMENT_SUFFIX}")
    _run_ffmpeg([
        ffmpeg_exe(), "-y", "-f", "concat", "-safe", "0", "-i", listing, "-c", "copy", joined
    ])
    _remove_quietly(listing)
    return joined


# ------------------------------------------------------------------- plumbing


def _add_input(
    inputs: list[str],
    path: str,
    *,
    start: float,
    duration: float,
    still: bool = False,
    loop: bool = False,
) -> int:
    """Append a seeked input and return its index.

    Seeking at the input rather than trimming in the graph is the whole reason
    a montage spread across a long source is affordable: the decoder only
    touches the frames that end up on screen.

    A still has nothing to seek into and no duration of its own, so it is
    looped for as long as it needs to be on screen instead. `loop` does the
    same for a video: a thirty-second background under a ninety-second clip
    plays three times rather than running out and leaving a black half-frame.
    Looping a file that is already long enough costs nothing — `-t` still ends
    the input at the same place.
    """
    index = sum(1 for item in inputs if item == "-i")
    length = filters.flt(max(0.01, duration))
    if still:
        inputs.extend(["-loop", "1", "-t", length, "-i", path])
        return index
    if loop:
        inputs.append("-stream_loop")
        inputs.append("-1")
    inputs.extend(["-ss", filters.flt(max(0.0, start)), "-t", length, "-i", path])
    return index


def _write_subtitles(
    composition: comp.Composition, output_path: str
) -> tuple[str | None, int]:
    spec = composition.subtitles
    if spec is None or spec.is_empty:
        return None, 0
    path = os.path.abspath(
        os.path.join(media_root(), "tmp", f"{Path(output_path).stem}.ass")
    )
    subtitles.write_ass_file(
        list(spec.cues),
        path,
        width=composition.canvas.width,
        height=composition.canvas.height,
        style=composition.style.subtitles,
        title_text=spec.title_text,
        title_end_sec=composition.duration_sec,
    )
    return path, len(spec.cues)


def _run_with_encoder_fallback(build: Callable[[str], list[str]], *, label: str) -> None:
    encoder = encoders.resolve(get_settings().render.video_encoder)
    try:
        _run_ffmpeg(build(encoder))
    except subprocess.CalledProcessError:
        if encoder == encoders.SOFTWARE_ENCODER:
            raise
        # A hardware encoder can pass a probe and still refuse a particular
        # frame size or filter output. Losing the clip over that would be worse
        # than losing the speed.
        log.warning("%s failed during %s; retrying with %s",
                    encoder, label, encoders.SOFTWARE_ENCODER)
        _run_ffmpeg(build(encoders.SOFTWARE_ENCODER))


def _run_ffmpeg(args: list[str]) -> None:
    """Run ffmpeg, raising CalledProcessError with its stderr attached."""
    subprocess.run(
        args,
        check=True,
        capture_output=True,
        timeout=get_settings().render.render_timeout_seconds,
    )


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:  # pragma: no cover - filesystem edge case
        pass


