"""Turning anchors into seconds, once the clip's length is known.

Every time in a scenario is an intention rather than a number, so this is where
the intentions meet a particular clip. Four of the six modes are arithmetic on
the clip's length; `event` asks the material; `after`/`before` ask another
element, which is the only one that needs an order.

That order is a topological sort, and a cycle in it is a validation error
rather than something the renderer discovers. Two elements each placed after
the other is a scenario somebody can save by accident in an editor, and the
difference between refusing it on save and refusing it on render is the
difference between a message naming two elements and a worker that never
returns.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.scenario import facts as fact_module
from app.domain.scenario import model
from app.domain.subtitles import SubtitleCue


class CycleError(ValueError):
    """`after`/`before` references that chase each other in a loop."""


@dataclass(frozen=True)
class Span:
    """When one element is on screen, in output seconds."""

    start_sec: float
    duration_sec: float

    @property
    def end_sec(self) -> float:
        return round(self.start_sec + self.duration_sec, 3)


def order(elements: tuple[model.Element, ...]) -> tuple[model.Element, ...]:
    """Elements sorted so nothing is placed before what it refers to.

    Kahn's algorithm, stable: elements with nothing to wait for keep the order
    the scenario wrote them in, so a scenario with no references compiles in
    exactly the order the editor shows.
    """
    by_id = {element.id: element for element in elements}
    waiting_for = {
        element.id: {
            ref for ref in _references(element) if ref in by_id and ref != element.id
        }
        for element in elements
    }

    ready = [element for element in elements if not waiting_for[element.id]]
    ordered: list[model.Element] = []
    while ready:
        element = ready.pop(0)
        ordered.append(element)
        for other in elements:
            if element.id in waiting_for[other.id]:
                waiting_for[other.id].discard(element.id)
                if not waiting_for[other.id] and other not in ordered + ready:
                    ready.append(other)

    if len(ordered) != len(elements):
        stuck = sorted(set(by_id) - {element.id for element in ordered})
        raise CycleError(
            "these elements are placed relative to each other in a loop, so "
            f"none of them has a time: {', '.join(stuck)}"
        )
    return tuple(ordered)


def _references(element: model.Element) -> set[str]:
    refs = {
        anchor.ref
        for anchor in (element.start, element.duration.until)
        if anchor is not None and anchor.ref
    }
    # A self-referencing blur is the same loop by another spelling.
    if element.slot.kind == model.SLOT_BLUR_OF and element.slot.ref:
        refs.add(element.slot.ref)
    return refs


def resolve(
    anchor: model.Anchor,
    *,
    clip_duration_sec: float,
    facts: fact_module.ClipFacts,
    placed: dict[str, Span],
    cues: tuple[SubtitleCue, ...] = (),
) -> tuple[float, str]:
    """The second this anchor means, and a note if it could not be honoured.

    Clamped into the clip rather than refused: an anchor two seconds before the
    end of a one-second clip is a scenario meeting unusually short material,
    which is normal, and the answer is the nearest time that exists.
    """
    note = ""
    if anchor.mode == model.AnchorMode.START:
        at = anchor.value
    elif anchor.mode == model.AnchorMode.END:
        at = clip_duration_sec - anchor.value
    elif anchor.mode == model.AnchorMode.FRACTION:
        at = clip_duration_sec * anchor.value
    elif anchor.mode == model.AnchorMode.EVENT:
        at, note = _event(anchor.event, facts=facts, cues=cues)
    else:
        at, note = _relative(anchor, placed)

    at += anchor.offset_sec
    clamped = min(max(0.0, at), max(0.0, clip_duration_sec))
    if not note and abs(clamped - at) > 1e-6:
        note = (
            f"anchor {anchor.mode.value} resolved to {at:.2f}s, outside a "
            f"{clip_duration_sec:.2f}s clip; moved to {clamped:.2f}s"
        )
    return round(clamped, 3), note


def _relative(anchor: model.Anchor, placed: dict[str, Span]) -> tuple[float, str]:
    span = placed.get(anchor.ref or "")
    if span is None:
        return 0.0, (
            f"element {anchor.ref!r} was not placed, so what follows it has "
            "nothing to follow; moved to the start"
        )
    if anchor.mode == model.AnchorMode.AFTER:
        return span.end_sec, ""
    return span.start_sec, ""


def _event(
    event: model.EventRef | None,
    *,
    facts: fact_module.ClipFacts,
    cues: tuple[SubtitleCue, ...] = (),
) -> tuple[float, str]:
    """Where in this clip the material says the event happens.

    Everything here is in output time. The cues are passed in rather than read
    off the facts because a cue is timed against the spine: dropping a pause
    moves every word after it, so word timings only mean something once the
    spine has been laid out.
    """
    if event is None:
        return 0.0, ""

    if event.kind in ("cut", "silence"):
        return _nth(facts.cuts, event.index, event.kind)
    if event.kind == "sentence":
        starts = tuple(cue.start_sec for cue in cues)
        return _nth(starts, event.index, "sentence")
    if event.kind == "word":
        matches = tuple(
            cue.start_sec for cue in cues
            if event.word.casefold() in cue.text.casefold()
        )
        if not matches:
            return 0.0, f"nothing in this clip says {event.word!r}; moved to the start"
        return _nth(matches, event.index, f"the word {event.word!r}")
    if event.kind == "loudest":
        if not facts.loudness:
            return 0.0, "no loudness curve for this clip; moved to the start"
        return max(facts.loudness, key=lambda point: point[1])[0], ""
    return 0.0, f"event {event.kind!r} cannot be measured yet; moved to the start"


def _nth(values: tuple[float, ...], index: int, what: str) -> tuple[float, str]:
    if not values:
        return 0.0, f"this clip has no {what}; moved to the start"
    try:
        return float(values[index]), ""
    except IndexError:
        position = index if index >= 0 else len(values) + index
        return (
            float(values[-1 if index >= 0 else 0]),
            f"this clip has {len(values)} of {what}, not {position + 1}; "
            "used the nearest",
        )
