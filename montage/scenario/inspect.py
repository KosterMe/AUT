"""A scenario taken apart on one clip, for somebody to look at.

The editor draws this. Not the EDL: an EDL says what to render and has
deliberately forgotten which element each layer came from, so an editor built
on it could not tell you *why* anything is where it is. And not a second
traversal of the scenario either — working the placements out again beside the
compiler is how an editor comes to draw one thing while the renderer does
another (§8.3 refuses a browser-side renderer for exactly this reason).

So: one compile, and this reads what it worked out. Everything here is
derived, nothing is decided.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from montage import composition as comp
from montage.scenario import compiler, facts as fact_module, model


@dataclass(frozen=True)
class Rect:
    """Where an element sits, in percent of the canvas."""

    x: float = 50.0
    y: float = 50.0
    width: float = 100.0
    height: float = 100.0
    fit: str = model.FIT_AUTO


@dataclass(frozen=True)
class Block:
    """One element of the scenario, where it landed on this clip."""

    element_id: str
    track_id: str
    track_kind: str
    label: str
    slot_kind: str
    slot_tag: str
    at_sec: float
    duration_sec: float
    # The anchor's mode, so the timeline can show what the block is held to
    # rather than only where it ended up: an element pinned to the end is
    # pinned to the right edge, visibly (§8.2).
    anchor: str
    frame: Rect
    z: int = 0
    optional: bool = False
    # Placed at all. A block that is not placed was dropped — by the overflow
    # policy on a short clip, or because nothing could fill its slot — and the
    # editor has to show it, because this is the mistake the layout switcher
    # exists to catch.
    placed: bool = True
    note: str = ""

    @property
    def end_sec(self) -> float:
        return round(self.at_sec + self.duration_sec, 3)


@dataclass(frozen=True)
class Ghost:
    """One thing a rule made on this clip.

    Drawn dashed: it is where the rule fired on *this* material, and on the
    next clip it will be somewhere else. That is the honest thing to show, and
    it is why these are separate from blocks rather than mixed in with them.
    """

    kind: str            # layer | audio
    at_sec: float
    duration_sec: float
    source_path: str = ""


@dataclass(frozen=True)
class RuleReport:
    """A rule, and what it did here."""

    element_id: str
    rule: str
    track_id: str
    label: str
    limit: int
    ghosts: tuple[Ghost, ...] = ()


@dataclass(frozen=True)
class Report:
    """A whole scenario laid out on one clip."""

    # Three lengths, and they are three different questions: what the clip
    # offered, how long the scenario laid itself out to be, and how long the
    # thing that will actually be rendered is. They agree on a scenario this
    # renderer can draw entirely; where they do not, the warnings say why.
    material_sec: float
    timeline_sec: float
    duration_sec: float
    canvas: comp.Canvas
    layout: str
    blocks: tuple[Block, ...] = ()
    rules: tuple[RuleReport, ...] = ()
    warnings: tuple[compiler.CompileWarning, ...] = ()
    subtitle_count: int = 0
    composition: comp.Composition | None = field(default=None, repr=False)


def inspect(scenario: model.Scenario, facts: fact_module.ClipFacts) -> Report:
    """Compile this scenario against these facts and report what happened."""
    made = compiler.plan(scenario, facts)
    notes = _by_element(made.warnings)

    blocks: list[Block] = []
    rules: list[RuleReport] = []
    for z, track in enumerate(scenario.tracks):
        for element in track.elements:
            if isinstance(element, model.RuleElement):
                rules.append(_rule(element, track, made))
            else:
                blocks.append(_block(element, track, made, z, notes))

    return Report(
        material_sec=facts.duration_sec,
        timeline_sec=made.timeline_sec,
        duration_sec=made.composition.duration_sec,
        canvas=scenario.canvas,
        layout=made.layout,
        blocks=tuple(blocks),
        rules=tuple(rules),
        warnings=made.warnings,
        subtitle_count=len(made.composition.subtitles.cues) if made.composition.subtitles else 0,
        composition=made.composition,
    )


def _block(
    element: model.Element,
    track: model.Track,
    made: compiler.Plan,
    z: int,
    notes: dict[str, str],
) -> Block:
    span = made.spans.get(element.id)
    placed = span is not None and span.duration_sec > 0 and not track.muted
    return Block(
        element_id=element.id,
        track_id=track.id,
        track_kind=track.kind,
        label=element.label or element.id,
        slot_kind=element.slot.kind,
        slot_tag=element.slot.tag,
        at_sec=span.start_sec if span else 0.0,
        duration_sec=span.duration_sec if span else 0.0,
        anchor=element.start.mode.value,
        frame=_frame(element, track, made),
        z=track.z or z,
        optional=element.optional,
        placed=placed,
        note=notes.get(element.id, "") or (_dropped(track, placed)),
    )


def _frame(element: model.Element, track: model.Track, made: compiler.Plan) -> Rect:
    """The rectangle this element occupies — as rendered, not as written.

    For everything but the spine those are the same thing. The spine is still
    a layout rather than a rectangle: the compiler reads the element's `fit`
    and gives every segment the rectangle that layout names, dropping whatever
    rectangle the element carried (trap 32). Showing the element's own numbers
    here would make the editor draw a spine the renderer will not produce.
    """
    if track.kind == model.TRACK_SPINE:
        frame = comp.frame_for_layout(made.layout)
        return Rect(frame.x, frame.y, frame.width, frame.height, frame.fit)
    own = element.frame
    return Rect(
        x=own.x.static, y=own.y.static,
        width=own.width.static, height=own.height.static,
        fit=own.fit,
    )


def _rule(element: model.RuleElement, track: model.Track, made: compiler.Plan) -> RuleReport:
    return RuleReport(
        element_id=element.id,
        rule=element.rule,
        track_id=track.id,
        label=element.label or element.rule,
        limit=element.limit,
        ghosts=tuple(
            Ghost(item.kind, item.at_sec, item.duration_sec, item.source_path)
            for item in made.produced if item.rule_id == element.id
        ),
    )


def _by_element(warnings: tuple[compiler.CompileWarning, ...]) -> dict[str, str]:
    """The first thing said about each element. The rest stay in `warnings`."""
    said: dict[str, str] = {}
    for warning in warnings:
        if warning.element_id and warning.element_id not in said:
            said[warning.element_id] = warning.message
    return said


def _dropped(track: model.Track, placed: bool) -> str:
    if placed:
        return ""
    if track.muted:
        return "дорожка выключена"
    return "не поместился на этой длительности"
