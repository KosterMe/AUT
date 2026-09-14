"""`compile(scenario, facts)` — a montage plus a clip, giving an EDL.

A scenario is a pure function of the facts about a clip, and this is that
function. It emits the `Composition` the renderer already understands, because
the renderer is not what is changing yet: the model and the compiler come
first, under golden tests, and the layered `Composition` v2 comes after them.

The order of resolution is fixed and not arbitrary — each step reads the one
before it:

1. **Spine.** Fixed lengths take theirs, elastic ones share the rest. This is
   where the clip's own length comes from, and everything else is measured
   against it.
2. **Frame.** `fit: auto` meets the source's real shape.
3. **Anchors.** Every intention becomes a second, in topological order so
   `after`/`before` have something to point at.
4. **Slots.** Promises become paths: an asset picked by tag, a blur of another
   element, a window of the source.
5. **Emission.** The `Composition` is assembled.
6. **Rules.** The automation expands against what now exists.

Step 6 is last and the sketch (§6) has it second. That is not a liberty: a rule
places b-roll against the clip's subtitle cues and a sound against its joins,
and neither exists until the spine is laid out and the cues are timed against
it. What makes the reordering safe rather than merely convenient is the
invariant an insert already carries — an overlay cannot move the picture
underneath it — so nothing a rule adds can feed back into steps 1 to 5. See
trap 21.

Warnings accumulate instead of raising. A scenario meeting unusually short
material, a library with no asset under the tag that was asked for, an outro
that did not fit — these are things to tell the operator about a clip that was
still made, and losing the clip to say them would be a poor trade.
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass

from montage.rules import audio as audio_planner
from montage import composition as comp
from montage.rules import inserts as insert_planner
from montage import subtitles as subtitle_builder
from montage.scenario import (
    anchors,
    curve as curve_module,
    facts as fact_module,
    layout as spine_layout,
    model,
)
from montage import style as style_module
from montage.style import (
    LAYOUT_AUTO,
    LAYOUT_BLUR,
    LAYOUT_FILL,
    LAYOUT_SPLIT,
)


@dataclass(frozen=True)
class CompileWarning:
    """Something worth telling the operator about a clip that was still made.

    Named `CompileWarning` rather than the sketch's `Warning` because that is
    a builtin, and shadowing it inside a module that also raises is the kind of
    saving nobody thanks you for.
    """

    code: str
    message: str
    element_id: str = ""


@dataclass(frozen=True)
class Produced:
    """One thing a rule made, and when it put it there.

    Drawn as a ghost in the editor: its position is what the rule did on *this*
    material, and on the next clip it will be somewhere else.
    """

    rule_id: str
    kind: str            # layer | audio
    at_sec: float
    duration_sec: float
    source_path: str = ""


@dataclass(frozen=True)
class Plan:
    """One compile with its working shown.

    `compile` returns the EDL and the warnings, because that is everything a
    render needs. An editor needs the rest of it — where each element landed,
    which layout the frame resolved to, what the rules made — and working that
    out a second time in a second place is exactly how an editor comes to draw
    something the renderer does not do. So there is one traversal and two
    views of it.
    """

    composition: comp.Composition
    warnings: tuple[CompileWarning, ...]
    # Element id → when it is on screen. The spine is in here too.
    spans: dict[str, anchors.Span]
    # How long the scenario laid itself out to be. Not the composition's
    # duration: an element this renderer cannot draw yet (a colour, a text
    # slot) takes its place on the timeline and contributes nothing to the
    # file, and an editor that showed only the second number would draw a
    # timeline that does not match its own blocks.
    timeline_sec: float
    # The layout the frame resolved to, in v1's vocabulary (§3.1).
    layout: str
    produced: tuple[Produced, ...] = ()
    # Element id → the rectangle it was actually given, motion and all. What
    # the editor draws: the EDL has forgotten which element each layer came
    # from, and working the geometry out a second time beside the compiler is
    # how a canvas comes to show a frame the renderer will not produce.
    frames: dict[str, comp.Frame] = dataclasses.field(default_factory=dict)
    # Element id → property → its keys, resolved to seconds. The editor draws
    # and drags these; they are resolved here because they were resolved here
    # anyway, and doing it twice is how a diamond ends up at a second the
    # renderer does not use.
    keys: dict[str, dict[str, tuple[tuple[float, float, str], ...]]] = dataclasses.field(
        default_factory=dict
    )


def compile(
    scenario: model.Scenario, facts: fact_module.ClipFacts
) -> tuple[comp.Composition, tuple[CompileWarning, ...]]:
    """This scenario applied to this clip."""
    made = plan(scenario, facts)
    return made.composition, made.warnings


def plan(scenario: model.Scenario, facts: fact_module.ClipFacts) -> Plan:
    """The same compile, keeping what it worked out along the way."""
    notes: list[CompileWarning] = []
    # What this clip has already put on screen. One set for the whole compile,
    # because "once per clip" is a property of the clip and not of one track.
    used: set[str] = set()

    # Element id → the rectangle it was given. Filled as the spine and the
    # layers are emitted, so the editor can be shown what the renderer got
    # rather than a second opinion about it.
    frames: dict[str, comp.Frame] = {}
    keys: dict[str, dict[str, tuple[tuple[float, float, str], ...]]] = {}

    spine = _spine_elements(scenario, notes)
    laid = spine_layout.lay_out(
        spine,
        available_sec=_material_sec(facts),
        natural_lookup=lambda element: _natural(element, facts),
    )
    _report_layout(laid, notes)

    frame_layout = _layout_of(scenario, facts)
    companion = _companion(scenario, facts, used) if frame_layout == LAYOUT_SPLIT else None
    if frame_layout == LAYOUT_SPLIT and not companion:
        # Asked for a split screen with nothing for the bottom half. The clip
        # is still worth making, so it falls back rather than failing.
        notes.append(CompileWarning(
            "no_companion",
            "a split screen was asked for but the library has nothing to put "
            "under the speaker; used a blurred backdrop instead",
        ))
        frame_layout = LAYOUT_BLUR

    segments = _segments(
        laid, facts, layout=frame_layout, notes=notes, used=used, frames=frames,
    )
    if not segments:
        notes.append(CompileWarning(
            "empty_spine", "the spine placed nothing; used the whole clip as one segment"
        ))
        segments = comp.single_source(
            facts.source_path, start_sec=facts.start_sec, end_sec=facts.end_sec,
            frame=comp.frame_for_layout(frame_layout),
            backdrop=frame_layout == LAYOUT_BLUR,
        ).spine

    draft = comp.Composition(
        spine=segments,
        canvas=scenario.canvas,
        style=scenario.style,
    )
    # Before the anchors, because an anchor on a spoken word is in output time
    # and a cue is what puts it there.
    cues = _cues(scenario, draft, facts)
    spans = _spans(scenario, laid, facts, cues, notes)
    draft = dataclasses.replace(
        draft,
        layers=_bottom_half(companion, draft) + _layers(
            scenario, spans, facts, notes, used,
            clip_duration_sec=draft.duration_sec, cues=cues, frames=frames, keys=keys,
        ),
        audio=_audio(scenario, spans, facts, notes, used),
        subtitles=_subtitle_spec(scenario, cues, facts),
    )
    produced: list[Produced] = []
    return Plan(
        frames=frames,
        keys=keys,
        composition=_apply_rules(scenario, draft, facts, notes, produced, used),
        warnings=tuple(notes),
        spans=spans,
        timeline_sec=laid.duration_sec,
        layout=frame_layout,
        produced=tuple(produced),
    )


def _bottom_half(companion: str | None, draft: comp.Composition) -> tuple[comp.Layer, ...]:
    """A split screen's lower half: one layer across the clip.

    Not a second source on every segment, which is what it used to be. The
    footage down there runs continuously and the per-segment cursor existed
    only because a segment was the only place to keep it.
    """
    if not companion:
        return ()
    return (comp.Layer(
        source_path=companion,
        at_sec=0.0,
        duration_sec=draft.duration_sec,
        frame=comp.BOTTOM_HALF,
        z=-1,
    ),)


# --- 1. the spine -----------------------------------------------------------


def _material_sec(facts: fact_module.ClipFacts) -> float:
    """How much material the spine has to share out.

    The kept length, not the window: removing a pause shortens the clip, and
    the spine's job is to produce the *output* length — which is what every
    anchor outside it is then measured against. Laying out over the raw window
    puts "two seconds before the end" two seconds before an end the clip never
    reaches.
    """
    if facts.keep:
        return round(sum(max(0.0, end - start) for start, end in facts.keep), 3)
    return facts.duration_sec


def _spine_elements(
    scenario: model.Scenario, notes: list[CompileWarning]
) -> tuple[model.Element, ...]:
    track = scenario.spine
    if track is None:
        notes.append(CompileWarning(
            "no_spine", "this scenario has no spine, so the clip has no length of its own"
        ))
        return ()
    kept: list[model.Element] = []
    for element in track.elements:
        if isinstance(element, model.RuleElement):
            notes.append(CompileWarning(
                "rule_on_spine",
                "a rule cannot sit on the spine: it would change how long the "
                "clip is, and everything anchored to the clip would move",
                element.id,
            ))
            continue
        kept.append(element)
    return tuple(kept)


def _natural(element: model.Element, facts: fact_module.ClipFacts) -> float | None:
    """How long this element's own material runs.

    The source's natural length is the clip window. An asset's is its own, and
    that needs the asset — which step 4 picks. Resolving the pick here for the
    spine only is the smallest way out of that circle, and it is legal because
    picking by tag reads the facts and nothing the layout produces.
    """
    if element.slot.kind in (model.SLOT_SOURCE, model.SLOT_BLUR_OF):
        return _material_sec(facts)
    chosen = _pick(element.slot, facts)
    return chosen.duration_sec if chosen and chosen.duration_sec else None


def _report_layout(laid: spine_layout.SpineLayout, notes: list[CompileWarning]) -> None:
    for dropped in laid.dropped:
        notes.append(CompileWarning(
            "dropped", "this clip is too short to fit everything; left it out", dropped
        ))
    if laid.overflow_sec > 0:
        notes.append(CompileWarning(
            "truncated",
            f"the spine asks for {laid.overflow_sec:.2f}s more than this clip "
            "has; the end is cut short",
        ))


# --- 2. the frame -----------------------------------------------------------


def _layout_of(scenario: model.Scenario, facts: fact_module.ClipFacts) -> str:
    """How the source fills the canvas, in the vocabulary v1 has for it.

    v1 says "layout" where the scenario says a fit mode and a background
    element, so this is the translation between them, and it is temporary:
    layers dissolve layouts in v2 (§3.1). An explicit request is honoured as
    it always was; `auto` is decided on shape, which is the rule
    `choose_layout` applied — now a value the scenario carries rather than a
    branch in the compiler.
    """
    requested = scenario.style.framing.layout
    if requested and requested != LAYOUT_AUTO:
        return requested

    spine = scenario.spine
    element = next(
        (e for e in (spine.elements if spine else ()) if isinstance(e, model.Element)), None
    )
    fit = element.frame.fit if element else model.FIT_AUTO
    if fit in (model.FIT_FILL, model.FIT_NONE):
        return LAYOUT_FILL
    if fit == model.FIT_COVER and not _has_backdrop(scenario):
        # Cover crops to fill, and with nothing behind it there is nothing for
        # a backdrop to show.
        return LAYOUT_FILL

    if not facts.width or not facts.height:
        return LAYOUT_BLUR
    source_aspect = float(facts.width) / float(facts.height)
    canvas_aspect = scenario.canvas.width / scenario.canvas.height
    tolerance = scenario.style.framing.fill_tolerance
    if source_aspect <= canvas_aspect * (1.0 + tolerance):
        # Already about as tall and narrow as the canvas: cropping it to fill
        # beats giving a vertical video a blurred backdrop made of itself.
        return LAYOUT_FILL
    return LAYOUT_BLUR


def _has_backdrop(scenario: model.Scenario) -> bool:
    return any(
        isinstance(element, model.Element) and element.slot.kind == model.SLOT_BLUR_OF
        for element in scenario.elements
    )


def _companion(
    scenario: model.Scenario, facts: fact_module.ClipFacts, used: set[str]
) -> str | None:
    """What plays in the bottom half of a split screen."""
    for element in scenario.elements:
        if isinstance(element, model.Element) and element.slot.kind == model.SLOT_LIBRARY:
            chosen = _pick(element.slot, facts, used)
            if chosen is not None and not chosen.audio:
                used.add(chosen.path)
                return chosen.path
    tag = scenario.style.framing.companion_tag
    chosen = _pick(model.Slot(kind=model.SLOT_LIBRARY, tag=tag), facts, used) if tag else None
    if chosen is not None:
        used.add(chosen.path)
        return chosen.path
    return None


# --- 3. anchors -------------------------------------------------------------


def _spans(
    scenario: model.Scenario,
    laid: spine_layout.SpineLayout,
    facts: fact_module.ClipFacts,
    cues: tuple,
    notes: list[CompileWarning],
) -> dict[str, anchors.Span]:
    """Every element's stretch of output time, spine included.

    The spine is seeded first because everything else may be anchored to it,
    and resolved in topological order so an element placed after another finds
    it already there.
    """
    placed: dict[str, anchors.Span] = {
        placement.element.id: anchors.Span(placement.start_sec, placement.duration_sec)
        for placement in laid.placements
    }
    duration = laid.duration_sec

    overlays = tuple(
        element for track in scenario.tracks if track.kind != model.TRACK_SPINE
        for element in track.elements
        if isinstance(element, model.Element) and not track.muted
    )
    try:
        ordered = anchors.order(overlays)
    except anchors.CycleError as exc:
        notes.append(CompileWarning("anchor_cycle", str(exc)))
        ordered = overlays

    for element in ordered:
        start, note = anchors.resolve(
            element.start, clip_duration_sec=duration, facts=facts,
            placed=placed, cues=cues,
        )
        if note:
            notes.append(CompileWarning("anchor", note, element.id))
        length, note = _length(element, start, duration, facts, placed, cues)
        if note:
            notes.append(CompileWarning("duration", note, element.id))
        placed[element.id] = anchors.Span(start, length)
    return placed


def _length(
    element: model.Element,
    start_sec: float,
    clip_duration_sec: float,
    facts: fact_module.ClipFacts,
    placed: dict[str, anchors.Span],
    cues: tuple = (),
) -> tuple[float, str]:
    """How long an overlay stays, clamped to what is left of the clip."""
    duration = element.duration
    if duration.mode == model.DurationMode.FIXED:
        length = float(duration.value)
    elif duration.mode == model.DurationMode.FRACTION:
        length = clip_duration_sec * float(duration.value)
    elif duration.mode == model.DurationMode.NATURAL:
        length = _natural(element, facts) or 0.0
    elif duration.mode == model.DurationMode.UNTIL:
        until, note = anchors.resolve(
            duration.until, clip_duration_sec=clip_duration_sec, facts=facts,
            placed=placed, cues=cues,
        )
        return round(max(0.0, until - start_sec), 3), note
    else:
        # Elastic off the spine has nothing to share, so it runs to the end.
        length = clip_duration_sec - start_sec

    if duration.min_sec:
        length = max(length, duration.min_sec)
    if duration.max_sec:
        length = min(length, duration.max_sec)

    room = max(0.0, clip_duration_sec - start_sec)
    if length > room:
        return round(room, 3), (
            f"asks for {length:.2f}s with {room:.2f}s of clip left; cut short"
        )
    return round(max(0.0, length), 3), ""


# --- 4. slots ---------------------------------------------------------------


def _pick(
    slot: model.Slot,
    facts: fact_module.ClipFacts,
    used: set[str] | None = None,
) -> insert_planner.AssetOption | None:
    """Which asset a library slot resolves to, deterministically.

    Rotation by last use is the existing behaviour and the reason one asset
    does not turn up in every clip of a job. Random is seeded by the clip, not
    by the clock: the same clip has to compile the same way twice or the
    fragment cache is inspecting something it has never seen.

    `used` carries what this clip has already put on screen, because one
    fragment appearing twice in one clip is the thing it most obviously must
    not do — the constraint the rules have always enforced, applied to an
    explicit slot as well. A library too small to satisfy every slot repeats
    rather than leaving them empty: the same shot twice beats a hole.
    """
    if slot.kind != model.SLOT_LIBRARY:
        return None
    candidates = [
        asset for asset in facts.assets
        if slot.tag in asset.tags or slot.tag == ""
    ]
    if not candidates:
        return None
    fresh = [asset for asset in candidates if asset.path not in (used or ())]
    candidates = fresh or candidates
    if slot.pick == model.PICK_FIRST:
        return candidates[0]
    if slot.pick == model.PICK_RANDOM:
        return candidates[facts.seed % len(candidates)]
    return min(candidates, key=lambda asset: (asset.last_used_rank, asset.asset_id))


# Which slots this compiler can turn into something, and what the installed
# ffmpeg has to be able to do for each. An empty tuple means nothing
# build-specific: a file is a file, and `color` is a source every build has.
#
# This is the list the editor is offered, so a slot missing from it is not
# offered rather than offered and dropped — which is what happened to a fill
# and a caption for three stages while the palette went on suggesting both
# (trap 59). Two are still absent and say why:
#
# * `gradient` — §1.1 names it and does not say what it is a gradient between,
#   and inventing the second colour is not a decision to take here.
# * `upload` — a `ClipFacts` has no uploads in it, so there is nothing to
#   resolve the id against. It needs a fact, not a filter.
SLOTS_DRAWN: dict[str, tuple[str, ...]] = {
    model.SLOT_SOURCE: (),
    model.SLOT_SOURCE_AT: (),
    model.SLOT_LIBRARY: (),
    model.SLOT_BLUR_OF: (),
    model.SLOT_COLOR: (),
    model.SLOT_TEXT: ("drawtext",),
}


def _path_of(
    element: model.Element,
    facts: fact_module.ClipFacts,
    notes: list[CompileWarning],
    used: set[str] | None = None,
) -> tuple[str, float, bool]:
    """The file this element plays, where in it to start, and whether it is a still.

    For the callers that need an actual file and can do nothing with a
    painting: a spine segment is cut out of material, and a sound has no
    picture to draw. `_source_of` is the one that also says what to paint.

    A painted element reaching one of those is not skipped quietly. It is the
    same shape as every other silent drop in this file — the montage renders,
    and the thing somebody put in it is simply not there.
    """
    path, at, still, paint = _source_of(element, facts, notes, used)
    if paint is not None:
        notes.append(CompileWarning(
            "drawn_not_played",
            f"a {element.slot.kind!r} slot is drawn rather than played, so it "
            "cannot be cut into the spine or heard on an audio track; put it "
            "on a layer",
            element.id,
        ))
        return "", 0.0, False
    return path, at, still


def _source_of(
    element: model.Element,
    facts: fact_module.ClipFacts,
    notes: list[CompileWarning],
    used: set[str] | None = None,
    *,
    style: "style_module.StyleSpec | None" = None,
) -> tuple[str, float, bool, comp.Paint | None]:
    """Where this element's picture comes from: a file, or a painting.

    Two slots have no file and never did — a flat fill and a line of text — and
    for three stages they compiled to a warning while the editor's palette went
    on offering both. §7.3 had reserved a second renderer for "graphics on
    top", so it looked like work waiting on a large piece of machinery. It was
    not: a fill is `color`, words are `drawtext`, and this build has both
    (trap 59).
    """
    slot = element.slot
    if slot.kind in (model.SLOT_SOURCE, model.SLOT_BLUR_OF):
        return facts.source_path, facts.start_sec, False, None
    if slot.kind == model.SLOT_SOURCE_AT:
        at, note = anchors._event(slot.event, facts=facts)
        if note:
            notes.append(CompileWarning("slot", note, element.id))
        return facts.source_path, round(facts.start_sec + at, 3), False, None
    if slot.kind == model.SLOT_LIBRARY:
        chosen = _pick(slot, facts, used)
        if chosen is None:
            notes.append(CompileWarning(
                "no_asset",
                f"the library has nothing tagged {slot.tag!r}, so this is left out",
                element.id,
            ))
            return "", 0.0, False, None
        if used is not None:
            used.add(chosen.path)
        return chosen.path, 0.0, chosen.still, None
    if slot.kind == model.SLOT_COLOR:
        return "", 0.0, False, comp.Paint(
            kind=comp.PAINT_COLOUR, colour=slot.color or "#000000",
        )
    if slot.kind == model.SLOT_TEXT:
        return "", 0.0, False, _lettering(element, facts, notes, style)
    notes.append(CompileWarning(
        "unsupported_slot",
        f"a {slot.kind!r} slot needs a layer of its own, which this renderer "
        "does not have yet; left out",
        element.id,
    ))
    return "", 0.0, False, None


def _lettering(
    element: model.Element,
    facts: fact_module.ClipFacts,
    notes: list[CompileWarning],
    style: "style_module.StyleSpec | None" = None,
) -> comp.Paint | None:
    """A text slot, with its template filled in against this clip.

    The substitution happens here and not in the renderer for the same reason
    anchors are resolved here: an EDL still holding `{title}` would be a plan
    rather than a description of a clip.

    The look is the one this project owns — the subtitle style — rather than a
    second set of type settings invented for the occasion. Size travels as a
    share of the canvas so a text layer survives a change of canvas the way a
    frame does.
    """
    filled, missing = _fill_template(element.slot.template, facts)
    if missing:
        # Rendering `{tags}` into the video is the quiet failure: it looks
        # deliberate. §1.1 lists it, and `ClipFacts` has nowhere to take it
        # from, so it is dropped and said out loud.
        notes.append(CompileWarning(
            "unknown_placeholder",
            "this clip knows nothing to put in " + ", ".join(sorted(missing))
            + "; those were left out of the text",
            element.id,
        ))
    if not filled.strip():
        notes.append(CompileWarning(
            "empty_text",
            "a text slot with nothing to say renders nothing; left out",
            element.id,
        ))
        return None
    look = (style or style_module.StyleSpec()).subtitles
    return comp.Paint(
        kind=comp.PAINT_TEXT,
        text=filled,
        colour=look.colour,
        # The subtitle size is in pixels of a 1920-tall canvas, which is the
        # only canvas this project has had. Carried as a share so a text layer
        # survives a change of one, the way a frame does.
        size_pct=round(look.font_size / 19.2, 2),
    )


def _fill_template(template: str, facts: fact_module.ClipFacts) -> tuple[str, set[str]]:
    """`{title}` and `{index}` against this clip, and whatever else was asked for."""
    known = {"title": facts.title or "", "index": str(facts.index)}
    missing: set[str] = set()

    def swap(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name in known:
            return known[name]
        missing.add("{" + name + "}")
        return ""

    return re.sub(r"\{(\w+)\}", swap, template).strip(), missing


# --- 5. emission ------------------------------------------------------------


def _segments(
    laid: spine_layout.SpineLayout,
    facts: fact_module.ClipFacts,
    *,
    layout: str,
    notes: list[CompileWarning],
    used: set[str],
    frames: dict[str, comp.Frame] | None = None,
) -> tuple[comp.Segment, ...]:
    """The spine as pieces of source, silence already taken out.

    The kept windows are consumed in order across the spine's elements, so a
    single elastic `source` element — the shape every clip has today — gives
    exactly the segments `composition.single_source` gives.

    How they fill the canvas is a rectangle — the element's own, when it has
    one. A spine that was left alone carries the default rectangle, and that
    is not "put the picture in the middle at full size": it means *the layout
    decides*, which is how `auto` still meets the shape of the source and how
    a scenario nobody has dragged anything in compiles to the graph it always
    did. Drag the spine and the rectangle becomes yours, `blur`/`fill`/`split`
    stop applying to it, and what is left of the canvas is still the
    backdrop's business.

    A split screen's bottom half is a layer across the whole clip rather than
    a second source on every segment — it is background, and background that
    jumps back on every cut above it draws the attention it is there not to
    draw.
    """
    windows = list(facts.keep) or [(0.0, facts.duration_sec)]
    segments: list[comp.Segment] = []

    for placement in laid.placements:
        element = placement.element
        path, base_sec, _ = _path_of(element, facts, notes, used)
        if not path:
            continue
        if element.slot.kind in (model.SLOT_SOURCE, model.SLOT_BLUR_OF):
            pieces, windows = _take(windows, placement.duration_sec)
        else:
            pieces = [(0.0, placement.duration_sec)]

        frame = _spine_frame(element.frame, layout, notes)
        if frames is not None:
            frames[element.id] = frame
        for rel_start, rel_end in pieces:
            if rel_end - rel_start < comp.MIN_SEGMENT_SECONDS:
                continue
            segments.append(comp.Segment(
                source_path=path,
                source_start_sec=round(base_sec + rel_start, 3),
                source_end_sec=round(base_sec + rel_end, 3),
                frame=frame,
                backdrop=layout == LAYOUT_BLUR,
            ))
    return tuple(segments)


def _spine_frame(
    frame: model.Frame, layout: str, notes: list[CompileWarning]
) -> comp.Frame:
    """Where a piece of the spine sits in the canvas.

    The default rectangle means the layout decides — see `_segments`. Anything
    else is taken as written, height included: a segment's height is a real
    number rather than the "let the aspect ratio work it out" a layer is
    allowed to leave behind, because there is nothing behind a segment to work
    it out against.

    A spine that moves is still refused, and not for lack of a rectangle: a
    segment is scaled and padded *before* the join, where `t` is the segment's
    own clock and not the clip's, so an expression there would animate every
    segment identically from its own zero. Layers are applied after the join,
    which is why they can move and this cannot.
    """
    if frame.fills_canvas:
        return comp.frame_for_layout(layout)
    if not frame.is_static:
        notes.append(CompileWarning(
            "no_spine_animation",
            "the spine cannot move: it is framed before the segments are "
            "joined, where every segment's clock starts again. Put what moves "
            "on a layer.",
        ))
    if frame.opacity.static != 1.0:
        # The spine is the bottom of the stack, so holding it back would mean
        # compositing it against nothing — and the renderer does not, which
        # until now it did not say. Layers fade; the thing they are laid over
        # does not (trap 52).
        notes.append(CompileWarning(
            "no_spine_opacity",
            "the spine is what everything else is laid over, so there is "
            "nothing for it to be transparent against; its opacity was "
            "ignored. Put what fades on a layer.",
        ))
    return comp.Frame(
        x=frame.x.static,
        y=frame.y.static,
        width=frame.width.static,
        height=frame.height.static,
        fit=frame.fit if frame.fit != model.FIT_AUTO else "cover",
        opacity=frame.opacity.static,
        rotate=frame.rotate.static,
    )


def _take(
    windows: list[tuple[float, float]], wanted_sec: float
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Cut `wanted_sec` of kept material off the front of `windows`."""
    taken: list[tuple[float, float]] = []
    remaining = round(wanted_sec, 3)
    rest = list(windows)

    while rest and remaining > 0:
        start, end = rest[0]
        length = max(0.0, end - start)
        if length <= remaining + 1e-9:
            taken.append((start, end))
            remaining = round(remaining - length, 3)
            rest.pop(0)
        else:
            taken.append((start, round(start + remaining, 3)))
            rest[0] = (round(start + remaining, 3), end)
            remaining = 0.0
    return taken, rest


def _overlays(scenario: model.Scenario, *kinds: str) -> tuple[tuple[model.Element, int], ...]:
    """Elements of the given track kinds, bottom of the stack first.

    Sorted by `z` because v1 has no z-order of its own: an insert is applied
    where it sits in the list, so the list order is the only place a track's z
    can go. Ties keep the order the scenario wrote them in, so two tracks at
    the same depth stack the way the editor shows them.
    """
    found = [
        (element, track.z, position)
        for position, track in enumerate(scenario.tracks)
        if track.kind in kinds and not track.muted
        for element in track.elements
        if isinstance(element, model.Element)
    ]
    found.sort(key=lambda item: (item[1], item[2]))
    return tuple((element, z) for element, z, _ in found)


def _layers(
    scenario: model.Scenario,
    spans: dict[str, anchors.Span],
    facts: fact_module.ClipFacts,
    notes: list[CompileWarning],
    used: set[str],
    *,
    clip_duration_sec: float = 0.0,
    cues: tuple = (),
    frames: dict[str, comp.Frame] | None = None,
    keys: dict[str, dict[str, tuple[tuple[float, float, str], ...]]] | None = None,
) -> tuple[comp.Layer, ...]:
    """Picture laid over the assembled video.

    A backdrop is not one of these. `blur_of(the source)` is the blurred
    backdrop the renderer already builds as part of a `blur` layout, so it is
    read as the layout it is rather than emitted a second time on top of it —
    which would put a blurred copy of the clip over the clip.
    """
    made: list[comp.Layer] = []
    for element, z in _overlays(scenario, model.TRACK_VIDEO, model.TRACK_OVERLAY):
        if element.slot.kind == model.SLOT_BLUR_OF:
            continue
        span = spans.get(element.id)
        if span is None or span.duration_sec <= 0:
            continue
        path, source_start, still, paint = _source_of(
            element, facts, notes, used, style=scenario.style,
        )
        if not path and paint is None:
            continue
        _warn_unrenderable(element, notes)
        length = clip_duration_sec or _clip_duration(spans)
        resolved_keys = {
            name: curve_module.resolved(
                value, clip_duration_sec=length, facts=facts, placed=spans, cues=cues,
            )
            for name, value in _animatable(element.frame)
        }
        frame = _edl_frame(
            element.frame, clip_duration_sec=length,
            facts=facts, placed=spans, cues=cues, keys=resolved_keys,
            # A painting has no shape of its own to fall back on, so the
            # height somebody typed is the only height there is.
            painted=paint is not None,
        )
        if frames is not None:
            frames[element.id] = frame
        if keys is not None and any(resolved_keys.values()):
            keys[element.id] = {
                name: value for name, value in resolved_keys.items() if value
            }
        made.append(comp.Layer(
            source_path=path,
            at_sec=span.start_sec,
            duration_sec=span.duration_sec,
            source_start_sec=source_start,
            still=still,
            frame=frame,
            z=z,
            paint=paint,
        ))
    return tuple(made)


def _edl_frame(
    frame: model.Frame,
    *,
    clip_duration_sec: float = 0.0,
    facts: fact_module.ClipFacts | None = None,
    placed: dict[str, anchors.Span] | None = None,
    cues: tuple = (),
    keys: dict[str, tuple[tuple[float, float, str], ...]] | None = None,
    painted: bool = False,
) -> comp.Frame:
    """The scenario's frame as the EDL's: what it came out as for this clip.

    Two `Frame`s on purpose. The scenario's carries `Animated` values, because
    a scenario is written before the clip exists; the EDL's is what those are
    worth once there is a clip — a number, or a polyline of numbers against
    the output's own clock. Anchored keyframes are resolved here for the same
    reason anchors are: an EDL that still held "half a second before the end"
    would be a plan rather than a description.

    Position moves; the rest is still flattened, and `_warn_unrenderable` says
    so. That split is measured, not assumed — see `Motion` and §7.2.
    """
    height = frame.height.static
    known = facts or fact_module.ClipFacts(end_sec=clip_duration_sec)
    animated = _animatable(frame)
    resolved = keys if keys is not None else {
        name: curve_module.resolved(
            value, clip_duration_sec=clip_duration_sec, facts=known,
            placed=placed, cues=cues,
        )
        for name, value in animated
    }
    motion = comp.Motion(**{
        name: curve_module.polyline(resolved.get(name, ()))
        for name, _ in animated
    })
    return comp.Frame(
        x=frame.x.static,
        y=frame.y.static,
        width=frame.width.static,
        # A frame covering the canvas keeps its height; a smaller one leaves it
        # to the aspect ratio, which is what a picture-in-picture always did.
        #
        # Unless there is no picture. A flat fill and a line of text have no
        # shape to fall back on, so "let the aspect ratio decide" would mean
        # "let the size of the ground the renderer happened to draw on decide"
        # — and a plate asked for at a seventh of the canvas came out running
        # off the bottom of it (trap 62).
        height=(
            height if painted or frame.fills_canvas or height >= 100.0 else 0.0
        ),
        fit=frame.fit if frame.fit != model.FIT_AUTO else "cover",
        opacity=frame.opacity.static,
        rotate=frame.rotate.static,
        motion=motion if motion.moves or motion.fades else None,
    )


def _animatable(frame: model.Frame) -> tuple[tuple[str, model.Animated], ...]:
    """The frame's curves, paired with the names `Motion` knows them by.

    One list, because the resolving happens in two places — once for the EDL
    and once for the editor's report — and a property animated in one of them
    and not the other is a difference nobody would see until a render.
    """
    return tuple(
        (name, getattr(frame, name)) for name in comp.CURVES
    )


def _warn_unrenderable(element: model.Element, notes: list[CompileWarning]) -> None:
    """Say what this renderer cannot yet carry, rather than dropping it quietly.

    Silence here would be the expensive kind of bug — a montage that renders
    successfully without the thing somebody put in it.

    Every property of the frame now animates, so what is left is per-element
    effects. The opacity warning that used to stand here was retired when the
    price it quoted turned out to be `geq`'s rather than opacity's (trap 50).
    """
    if element.effects:
        notes.append(CompileWarning(
            "no_effects",
            "per-element effects need a layer of their own; they were ignored",
            element.id,
        ))


def _audio(
    scenario: model.Scenario,
    spans: dict[str, anchors.Span],
    facts: fact_module.ClipFacts,
    notes: list[CompileWarning],
    used: set[str],
) -> tuple[comp.AudioTrack, ...]:
    """Every sound this scenario makes, in one list.

    A bed and a stinger are one structure with different values now, so there
    is one function rather than two — which is the point of §3.1's third
    paragraph: three entities that shared most of their fields are why adding
    a fourth meant a fourth code path.
    """
    bed = _bed(scenario, spans, facts, notes, used)
    stingers = _stingers(scenario, spans, facts, notes, used)
    return ((bed,) if bed is not None else ()) + stingers


def _bed(
    scenario: model.Scenario,
    spans: dict[str, anchors.Span],
    facts: fact_module.ClipFacts,
    notes: list[CompileWarning],
    used: set[str],
) -> comp.AudioTrack | None:
    """A bed under the whole clip: an audio element that loops and ducks."""
    for element, _ in _overlays(scenario, model.TRACK_AUDIO):
        if not (element.audio.enabled and element.audio.loop):
            continue
        chosen = _pick(element.slot, facts, used)
        path, _, _ = _path_of(element, facts, notes, used)
        if not path:
            continue
        policy = scenario.style.audio
        # v1 has no "ducked" flag: ducking is the sidechain compressor's
        # settings, and it is off when the compressor cannot trigger. A
        # threshold of 1.0 is above any signal, which is how an element that
        # says it is not ducked by speech says so in this vocabulary.
        ducking = dict(
            duck_threshold=policy.duck_threshold,
            duck_ratio=policy.duck_ratio,
            duck_attack_ms=policy.duck_attack_ms,
            duck_release_ms=policy.duck_release_ms,
        ) if element.audio.ducked_by_speech else {"duck_threshold": 1.0, "duck_ratio": 1.0}
        return comp.AudioTrack(
            source_path=path,
            loop=True,
            gain_db=element.audio.gain_db.static or policy.music_gain_db,
            # Where in the *track* to begin, not where in the clip: a long bed
            # always started from zero means every clip of a job opens on the
            # same four bars. Stepping in by a seeded amount costs nothing and
            # stays deterministic per clip — the same function the planner has
            # always used, because this is the same decision.
            source_start_sec=(
                audio_planner._offset_into(chosen, _clip_duration(spans), facts.seed)
                if chosen is not None else 0.0
            ),
            fade_in_sec=element.audio.fade_in_sec or policy.music_fade_in_sec,
            fade_out_sec=element.audio.fade_out_sec or policy.music_fade_out_sec,
            **ducking,
        )
    return None


def _clip_duration(spans: dict[str, anchors.Span]) -> float:
    """How long the clip runs, from the spans already resolved against it."""
    return round(max((span.end_sec for span in spans.values()), default=0.0), 3)


def _stingers(
    scenario: model.Scenario,
    spans: dict[str, anchors.Span],
    facts: fact_module.ClipFacts,
    notes: list[CompileWarning],
    used: set[str],
) -> tuple[comp.AudioTrack, ...]:
    """One-shot sounds: audio elements that do not loop."""
    made: list[comp.AudioTrack] = []
    for element, _ in _overlays(scenario, model.TRACK_AUDIO):
        if not element.audio.enabled or element.audio.loop:
            continue
        span = spans.get(element.id)
        if span is None:
            continue
        path, _, _ = _path_of(element, facts, notes, used)
        if not path:
            continue
        made.append(comp.AudioTrack(
            source_path=path,
            at_sec=span.start_sec,
            duration_sec=span.duration_sec or 1.0,
            gain_db=element.audio.gain_db.static,
        ))
    return tuple(made)


def _cues(
    scenario: model.Scenario, draft: comp.Composition, facts: fact_module.ClipFacts
) -> tuple:
    """The clip's words, timed against the spine that was just laid out.

    Built here and not taken from the facts because a cue is in output time:
    removing a pause moves every word after it, so cues cannot exist until the
    segments do. They are wanted twice over — by the subtitles and by any
    anchor that names a word — so they are built once, here, and passed on.
    """
    if not scenario.style.subtitles.enabled:
        return ()
    return tuple(subtitle_builder.make_subtitle_cues(
        list(facts.speech),
        timeline_segments=draft.timeline(),
        fallback_text=facts.title,
        style=scenario.style.subtitles,
    ))


def _subtitle_spec(
    scenario: model.Scenario, cues: tuple, facts: fact_module.ClipFacts
) -> comp.SubtitleSpec | None:
    if not scenario.style.subtitles.enabled:
        return None
    return comp.SubtitleSpec(cues=cues, title_text=facts.title or None)


# --- 6. rules ---------------------------------------------------------------


def _apply_rules(
    scenario: model.Scenario,
    composition: comp.Composition,
    facts: fact_module.ClipFacts,
    notes: list[CompileWarning],
    produced: list[Produced],
    used: set[str],
) -> comp.Composition:
    """Expand the automation against the clip that now exists.

    Two passes, and the split is not cosmetic. The first holds the rules that
    read only the clip; the second holds `on_every_cut`, which reads the joins
    of what the first pass produced — an insert appearing is one of the moments
    a transition sound belongs on, and it does not exist until now.

    `keyword_broll` is `inserts.choose_inserts` and `on_every_cut` is
    `audio.choose_effects`, with every limit the README earned intact — not
    over the hook, no more than one every six seconds, never more than a third
    of the clip, each fragment once per clip, rotation by last use. The other
    two place the rule's own template, and enforce the same limits from the
    rule rather than from a policy object (§5).
    """
    rules = [
        element for element in scenario.elements if isinstance(element, model.RuleElement)
    ]
    if not rules:
        return composition

    staged = composition
    for rule in rules:
        if rule.rule == model.RULE_KEYWORD_BROLL:
            if not facts.assets:
                continue
            chosen = insert_planner.choose_inserts(
                staged, assets=list(facts.assets),
                policy=_insert_policy(scenario, rule), seed=facts.seed,
            )
            if chosen:
                staged = dataclasses.replace(staged, layers=staged.layers + chosen)
                produced.extend(
                    Produced(rule.id, "layer", layer.at_sec, layer.duration_sec,
                             layer.source_path)
                    for layer in chosen
                )
        elif rule.rule == model.RULE_CADENCE:
            staged = _place(
                rule, staged, _cadence_times(rule, staged.duration_sec),
                facts=facts, notes=notes, used=used, produced=produced,
            )
        elif rule.rule == model.RULE_ON_LOUDEST:
            staged = _place(
                rule, staged, _loudest_times(rule, facts, staged.duration_sec),
                facts=facts, notes=notes, used=used, produced=produced,
            )

    for rule in rules:
        if rule.rule == model.RULE_ON_EVERY_CUT:
            if not facts.assets:
                continue
            effects = audio_planner.choose_effects(
                staged, assets=list(facts.assets), policy=scenario.style.audio,
                seed=facts.seed,
            )
            if effects:
                staged = dataclasses.replace(staged, audio=staged.audio + effects)
                produced.extend(
                    Produced(rule.id, "audio", effect.at_sec, effect.duration_sec,
                             effect.source_path)
                    for effect in effects
                )
        elif rule.rule not in (
            model.RULE_KEYWORD_BROLL, model.RULE_CADENCE, model.RULE_ON_LOUDEST,
        ):
            notes.append(CompileWarning(
                "unimplemented_rule",
                f"the {rule.rule!r} rule has no implementation yet; it did nothing",
                rule.id,
            ))

    return staged


def _cadence_times(rule: model.RuleElement, duration_sec: float) -> list[float]:
    """Every `every_sec` seconds, inside the guards.

    The simplest rule there is, and the one worth having for exactly that
    reason: a logo every thirty seconds is a thing people ask for, and writing
    it as four elements with four anchors is how a scenario stops surviving a
    clip of a different length.
    """
    every = float(rule.params.get("every_sec") or 15.0)
    if every <= 0:
        return []
    at = rule.guard_head_sec
    last = duration_sec - rule.guard_tail_sec
    times: list[float] = []
    while at <= last and len(times) < 500:
        times.append(round(at, 3))
        at += every
    return times


def _loudest_times(
    rule: model.RuleElement, facts: fact_module.ClipFacts, duration_sec: float
) -> list[float]:
    """The loudest moments first, in output time.

    The curve is measured over the clip, so a reading at clip-second 40 is at
    output-second 35 once a five-second pause has been cut out of the middle —
    and placing it at 40 would land it after the moment it was chosen for.
    `ClipFacts.output_time` does that mapping, and drops the readings that fall
    inside a pause that was removed: they are not in the clip at all.
    """
    if not facts.loudness:
        return []
    inside: list[tuple[float, float]] = []
    for at, level in facts.loudness:
        mapped = facts.output_time(at)
        if mapped is None:
            continue
        if mapped < rule.guard_head_sec or mapped > duration_sec - rule.guard_tail_sec:
            continue
        inside.append((level, mapped))
    # Loudest first, and among equally loud moments the earliest, so the same
    # clip compiles the same way twice.
    inside.sort(key=lambda pair: (-pair[0], pair[1]))
    return [at for _, at in inside]


def _place(
    rule: model.RuleElement,
    composition: comp.Composition,
    times: list[float],
    *,
    facts: fact_module.ClipFacts,
    notes: list[CompileWarning],
    used: set[str],
    produced: list[Produced],
) -> comp.Composition:
    """Put the rule's template at each of those moments, within its limits.

    A rule is an element that says how to make several, so what it makes is an
    element — its template. Without one it has nothing to place, and that is
    worth saying out loud rather than quietly doing nothing.
    """
    template = rule.template
    if template is None:
        notes.append(CompileWarning(
            "rule_without_template",
            f"the {rule.rule!r} rule has nothing to place: give it a template element",
            rule.id,
        ))
        return composition

    clip = composition.duration_sec
    budget = rule.max_share * clip if rule.max_share else clip
    layers: list[comp.Layer] = []
    sounds: list[comp.AudioTrack] = []
    spent = 0.0
    placed: list[float] = []

    for at in times:
        if rule.limit and len(placed) >= rule.limit:
            break
        if any(abs(at - taken) < rule.min_gap_sec for taken in placed):
            continue
        length, _ = _length(template, at, clip, facts, {}, ())
        if length <= 0:
            continue
        if spent + length > budget:
            continue
        path, source_start, still = _path_of(template, facts, notes, used)
        if not path:
            # The library has nothing for it. `_path_of` has already said so,
            # and saying it once per moment would bury everything else.
            break

        if template.audio.enabled:
            sounds.append(comp.AudioTrack(
                source_path=path, at_sec=at, duration_sec=length,
                gain_db=template.audio.gain_db.static,
            ))
            produced.append(Produced(rule.id, "audio", at, length, path))
        else:
            layers.append(comp.Layer(
                source_path=path, at_sec=at, duration_sec=length,
                source_start_sec=source_start, still=still,
                frame=_edl_frame(template.frame),
                z=_z_of(rule, composition),
            ))
            produced.append(Produced(rule.id, "layer", at, length, path))
        placed.append(at)
        spent += length

    if not layers and not sounds:
        return composition
    return dataclasses.replace(
        composition,
        layers=composition.layers + tuple(layers),
        audio=composition.audio + tuple(sounds),
    )


def _z_of(rule: model.RuleElement, composition: comp.Composition) -> int:
    """Above whatever is already there: a rule's output is an overlay."""
    return max((layer.z for layer in composition.layers), default=0) + 1


def _insert_policy(scenario: model.Scenario, rule: model.RuleElement):
    """The rule's own limits, over the style's."""
    return dataclasses.replace(
        scenario.style.inserts,
        enabled=True,
        max_inserts=rule.limit,
        min_gap_seconds=rule.min_gap_sec,
        hook_guard_seconds=rule.guard_head_sec,
        tail_guard_seconds=rule.guard_tail_sec,
        max_share=rule.max_share,
    )
