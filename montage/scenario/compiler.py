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
from dataclasses import dataclass

from montage.rules import audio as audio_planner
from montage import composition as comp
from montage.rules import inserts as insert_planner
from montage import subtitles as subtitle_builder
from montage.scenario import anchors, facts as fact_module, layout as spine_layout, model
from montage.style import (
    INSERT_FULL,
    INSERT_PIP,
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


def compile(
    scenario: model.Scenario, facts: fact_module.ClipFacts
) -> tuple[comp.Composition, tuple[CompileWarning, ...]]:
    """This scenario applied to this clip."""
    notes: list[CompileWarning] = []
    # What this clip has already put on screen. One set for the whole compile,
    # because "once per clip" is a property of the clip and not of one track.
    used: set[str] = set()

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
        laid, facts, layout=frame_layout, companion_path=companion,
        notes=notes, used=used,
    )
    if not segments:
        notes.append(CompileWarning(
            "empty_spine", "the spine placed nothing; used the whole clip as one segment"
        ))
        segments = comp.single_source(
            facts.source_path, start_sec=facts.start_sec, end_sec=facts.end_sec,
            layout=frame_layout, companion_path=companion,
        ).segments

    draft = comp.Composition(
        segments=segments,
        canvas=scenario.canvas,
        style=scenario.style,
    )
    # Before the anchors, because an anchor on a spoken word is in output time
    # and a cue is what puts it there.
    cues = _cues(scenario, draft, facts)
    spans = _spans(scenario, laid, facts, cues, notes)
    draft = dataclasses.replace(
        draft,
        inserts=_inserts(scenario, spans, facts, notes, used),
        music=_music(scenario, spans, facts, notes, used),
        effects=_effects(scenario, spans, facts, notes, used),
        subtitles=_subtitle_spec(scenario, cues, facts),
    )
    return _apply_rules(scenario, draft, facts, notes), tuple(notes)


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


def _path_of(
    element: model.Element,
    facts: fact_module.ClipFacts,
    notes: list[CompileWarning],
    used: set[str] | None = None,
) -> tuple[str, float, bool]:
    """The file this element plays, where in it to start, and whether it is a still."""
    slot = element.slot
    if slot.kind in (model.SLOT_SOURCE, model.SLOT_BLUR_OF):
        return facts.source_path, facts.start_sec, False
    if slot.kind == model.SLOT_SOURCE_AT:
        at, note = anchors._event(slot.event, facts=facts)
        if note:
            notes.append(CompileWarning("slot", note, element.id))
        return facts.source_path, round(facts.start_sec + at, 3), False
    if slot.kind == model.SLOT_LIBRARY:
        chosen = _pick(slot, facts, used)
        if chosen is None:
            notes.append(CompileWarning(
                "no_asset",
                f"the library has nothing tagged {slot.tag!r}, so this is left out",
                element.id,
            ))
            return "", 0.0, False
        if used is not None:
            used.add(chosen.path)
        return chosen.path, 0.0, chosen.still
    notes.append(CompileWarning(
        "unsupported_slot",
        f"a {slot.kind!r} slot needs a layer of its own, which this renderer "
        "does not have yet; left out",
        element.id,
    ))
    return "", 0.0, False


# --- 5. emission ------------------------------------------------------------


def _segments(
    laid: spine_layout.SpineLayout,
    facts: fact_module.ClipFacts,
    *,
    layout: str,
    companion_path: str | None,
    notes: list[CompileWarning],
    used: set[str],
) -> tuple[comp.Segment, ...]:
    """The spine as pieces of source, silence already taken out.

    The kept windows are consumed in order across the spine's elements, so a
    single elastic `source` element — the shape every clip has today — gives
    exactly the segments `composition.single_source` gives.

    A split screen's companion advances across segments rather than restarting
    on each: the bottom half is background, and background that jumps back on
    every cut above it draws the attention it is there not to draw.
    """
    windows = list(facts.keep) or [(0.0, facts.duration_sec)]
    segments: list[comp.Segment] = []
    companion_cursor = 0.0

    for placement in laid.placements:
        element = placement.element
        path, base_sec, _ = _path_of(element, facts, notes, used)
        if not path:
            continue
        if element.slot.kind in (model.SLOT_SOURCE, model.SLOT_BLUR_OF):
            pieces, windows = _take(windows, placement.duration_sec)
        else:
            pieces = [(0.0, placement.duration_sec)]

        for rel_start, rel_end in pieces:
            if rel_end - rel_start < comp.MIN_SEGMENT_SECONDS:
                continue
            segments.append(comp.Segment(
                source_path=path,
                source_start_sec=round(base_sec + rel_start, 3),
                source_end_sec=round(base_sec + rel_end, 3),
                layout=layout,
                companion_path=companion_path,
                companion_start_sec=round(companion_cursor, 3),
            ))
            companion_cursor += rel_end - rel_start
    return tuple(segments)


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


def _inserts(
    scenario: model.Scenario,
    spans: dict[str, anchors.Span],
    facts: fact_module.ClipFacts,
    notes: list[CompileWarning],
    used: set[str],
) -> tuple[comp.Insert, ...]:
    """Picture laid over the assembled video.

    A backdrop is not one of these. `blur_of(the source)` is the blurred
    backdrop the renderer already builds as part of a `blur` layout, so it is
    read as the layout it is rather than emitted a second time on top of it —
    which would put a blurred copy of the clip over the clip.
    """
    made: list[comp.Insert] = []
    for element, _ in _overlays(scenario, model.TRACK_VIDEO, model.TRACK_OVERLAY):
        if element.slot.kind == model.SLOT_BLUR_OF:
            continue
        span = spans.get(element.id)
        if span is None or span.duration_sec <= 0:
            continue
        path, source_start, still = _path_of(element, facts, notes, used)
        if not path:
            continue
        _warn_unrenderable(element, notes)
        made.append(comp.Insert(
            kind=INSERT_FULL if element.frame.fills_canvas else INSERT_PIP,
            source_path=path,
            at_sec=span.start_sec,
            duration_sec=span.duration_sec,
            source_start_sec=source_start,
            still=still,
        ))
    return tuple(made)


def _warn_unrenderable(element: model.Element, notes: list[CompileWarning]) -> None:
    """Say what this renderer cannot yet carry, rather than dropping it quietly.

    v1 has two insert shapes and no transforms, so an animated frame or a
    per-element effect compiles to the nearest thing it does have. Silence here
    would be the expensive kind of bug — a montage that renders successfully
    without the movement somebody put in it.
    """
    if not element.frame.is_static:
        notes.append(CompileWarning(
            "no_animation",
            "this renderer places an overlay but cannot move it yet; the "
            "keyframes were ignored",
            element.id,
        ))
    if element.effects:
        notes.append(CompileWarning(
            "no_effects",
            "per-element effects need a layer of their own; they were ignored",
            element.id,
        ))


def _music(
    scenario: model.Scenario,
    spans: dict[str, anchors.Span],
    facts: fact_module.ClipFacts,
    notes: list[CompileWarning],
    used: set[str],
) -> comp.MusicBed | None:
    """A bed under the whole clip: an audio element that loops and ducks."""
    for element, _ in _overlays(scenario, model.TRACK_AUDIO):
        if not (element.audio.enabled and element.audio.loop):
            continue
        path, _, _ = _path_of(element, facts, notes, used)
        if not path:
            continue
        span = spans.get(element.id, anchors.Span(0.0, 0.0))
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
        return comp.MusicBed(
            source_path=path,
            gain_db=element.audio.gain_db.static or policy.music_gain_db,
            start_sec=span.start_sec,
            fade_in_sec=element.audio.fade_in_sec or policy.music_fade_in_sec,
            fade_out_sec=element.audio.fade_out_sec or policy.music_fade_out_sec,
            **ducking,
        )
    return None


def _effects(
    scenario: model.Scenario,
    spans: dict[str, anchors.Span],
    facts: fact_module.ClipFacts,
    notes: list[CompileWarning],
    used: set[str],
) -> tuple[comp.SoundEffect, ...]:
    """One-shot sounds: audio elements that do not loop."""
    made: list[comp.SoundEffect] = []
    for element, _ in _overlays(scenario, model.TRACK_AUDIO):
        if not element.audio.enabled or element.audio.loop:
            continue
        span = spans.get(element.id)
        if span is None:
            continue
        path, _, _ = _path_of(element, facts, notes, used)
        if not path:
            continue
        made.append(comp.SoundEffect(
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
) -> comp.Composition:
    """Expand the automation against the clip that now exists.

    The two rules that already have implementations keep them: `keyword_broll`
    is `inserts.choose_inserts` and `on_every_cut` is `audio.choose_effects`,
    with every limit the README earned intact — not over the hook, no more than
    one every six seconds, never more than a third of the clip, each fragment
    once per clip, rotation by last use.

    B-roll is expanded before sound, deliberately: an insert appearing is one
    of the moments a transition sound belongs on, and it does not exist until
    now.
    """
    rules = [
        element for element in scenario.elements if isinstance(element, model.RuleElement)
    ]
    if not rules or not facts.assets:
        return composition

    assets = list(facts.assets)
    changes: dict = {}
    for rule in rules:
        if rule.rule == model.RULE_KEYWORD_BROLL:
            chosen = insert_planner.choose_inserts(
                composition, assets=assets,
                policy=_insert_policy(scenario, rule), seed=facts.seed,
            )
            if chosen:
                changes["inserts"] = composition.inserts + chosen

    staged = dataclasses.replace(composition, **changes) if changes else composition
    for rule in rules:
        if rule.rule == model.RULE_ON_EVERY_CUT:
            effects = audio_planner.choose_effects(
                staged, assets=assets, policy=scenario.style.audio, seed=facts.seed,
            )
            if effects:
                changes["effects"] = staged.effects + effects
        elif rule.rule not in (model.RULE_KEYWORD_BROLL,):
            notes.append(CompileWarning(
                "unimplemented_rule",
                f"the {rule.rule!r} rule has no implementation yet; it did nothing",
                rule.id,
            ))

    return dataclasses.replace(composition, **changes) if changes else composition


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
