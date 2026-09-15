"""Laying out the spine: flexbox, with seconds instead of pixels.

The spine is a row of boxes sharing a length nobody knew when the scenario was
written. Fixed elements take theirs, elastic ones share what is left in
proportion to `grow`, and both clamp to their own bounds. That is the same
problem a browser solves along the main axis, so it gets the same answer and
the same vocabulary.

What matters more than the happy path is the unhappy one. A scenario with a
3-second hook, a 5-second outro and an elastic middle is fine on a 90-second
clip and impossible on a 6-second one, and a short clip is normal material
rather than an error. So the policy is explicit and in this order:

1. elastic elements shrink, down to `min_sec`;
2. still too long — optional elements are dropped, lowest priority first;
3. still too long — the clip is rendered truncated and says so.

Step 3 is a warning and not a failure on purpose: losing an outro is better
than losing the clip.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from montage.scenario import model

# Below this an element is not worth a segment — it is shorter than the seek
# accuracy of most sources, and `composition.Segment` refuses it outright.
MIN_PIECE_SECONDS = 0.05

NaturalLookup = Callable[[model.Element], float | None]


@dataclass(frozen=True)
class Placement:
    """One spine element, resolved to a stretch of output time."""

    element: model.Element
    start_sec: float
    duration_sec: float

    @property
    def end_sec(self) -> float:
        return round(self.start_sec + self.duration_sec, 3)


@dataclass(frozen=True)
class SpineLayout:
    placements: tuple[Placement, ...] = ()
    # The clip's output length: what the spine actually filled, which is what
    # every anchor outside the spine is then resolved against.
    duration_sec: float = 0.0
    dropped: tuple[str, ...] = ()
    # How much of the spine did not fit. Non-zero means somebody's outro is
    # missing its tail and the result has to say so.
    overflow_sec: float = 0.0

    def at(self, element_id: str) -> Placement | None:
        return next((p for p in self.placements if p.element.id == element_id), None)


def lay_out(
    elements: tuple[model.Element, ...],
    *,
    available_sec: float,
    natural_lookup: NaturalLookup | None = None,
) -> SpineLayout:
    """Share `available_sec` between the spine's elements.

    `natural_lookup` answers how long an element's own material runs, for
    `DurationMode.NATURAL`. It is a callback rather than a field because the
    answer depends on which asset a slot resolved to, and an element with no
    answer is treated as elastic rather than as zero-length — an asset whose
    duration is unknown should stretch, not vanish.
    """
    if not elements:
        return SpineLayout(duration_sec=0.0)

    available = max(0.0, round(float(available_sec), 3))
    kept = list(elements)
    dropped: list[str] = []

    while True:
        fixed, elastic = _split(kept, available, natural_lookup)
        demanded = sum(fixed.values()) + sum(
            _floor(element) for element in elastic
        )
        if demanded <= available or not _droppable(kept):
            break
        # Shrinking the elastic elements was not enough, so something has to
        # go. Lowest priority first, and only what said it was expendable.
        victim = min(_droppable(kept), key=lambda e: (e.priority, kept.index(e)))
        kept.remove(victim)
        dropped.append(victim.id)

    return _place(kept, available, fixed, elastic, tuple(dropped))


def _split(
    elements: list[model.Element],
    available: float,
    natural_lookup: NaturalLookup | None,
) -> tuple[dict[str, float], list[model.Element]]:
    """Which elements have a length of their own, and which take the rest."""
    fixed: dict[str, float] = {}
    elastic: list[model.Element] = []

    for element in elements:
        length = _own_length(element, available, natural_lookup)
        if length is None:
            elastic.append(element)
        else:
            fixed[element.id] = _clamp(element, length)
    return fixed, elastic


def _own_length(
    element: model.Element, available: float, natural_lookup: NaturalLookup | None
) -> float | None:
    """This element's length before the remainder is shared, or None."""
    duration = element.duration
    if duration.mode == model.DurationMode.FIXED:
        return float(duration.value)
    if duration.mode == model.DurationMode.FRACTION:
        return available * float(duration.value)
    if duration.mode == model.DurationMode.NATURAL:
        natural = natural_lookup(element) if natural_lookup else None
        return float(natural) if natural else None
    # ELASTIC shares the remainder; UNTIL is resolved against other anchors,
    # which has not happened yet, so it stretches for now.
    return None


def _clamp(element: model.Element, length: float) -> float:
    duration = element.duration
    if duration.min_sec:
        length = max(length, duration.min_sec)
    if duration.max_sec:
        length = min(length, duration.max_sec)
    return max(0.0, round(length, 3))


def _floor(element: model.Element) -> float:
    """The least an elastic element will accept before it is dropped instead."""
    return max(element.duration.min_sec, 0.0)


def _droppable(elements: list[model.Element]) -> list[model.Element]:
    return [element for element in elements if element.optional]


def _place(
    elements: list[model.Element],
    available: float,
    fixed: dict[str, float],
    elastic: list[model.Element],
    dropped: tuple[str, ...],
) -> SpineLayout:
    """Turn lengths into consecutive stretches of output time."""
    lengths = dict(fixed)
    remainder = available - sum(fixed.values())
    lengths.update(_share(elastic, remainder))

    placements: list[Placement] = []
    cursor = 0.0
    for element in elements:
        length = lengths.get(element.id, 0.0)
        room = max(0.0, available - cursor)
        granted = min(length, room)
        if granted >= MIN_PIECE_SECONDS:
            placements.append(Placement(element, round(cursor, 3), round(granted, 3)))
            cursor += granted
        elif length >= MIN_PIECE_SECONDS:
            # It asked for real time and there is none left. Recorded as
            # overflow rather than placed at zero length, which no renderer
            # would accept anyway.
            pass

    demanded = sum(lengths.values())
    return SpineLayout(
        placements=tuple(placements),
        duration_sec=round(cursor, 3),
        dropped=dropped,
        overflow_sec=round(max(0.0, demanded - available), 3),
    )


def _share(elastic: list[model.Element], remainder: float) -> dict[str, float]:
    """Divide what is left between the elastic elements, by `grow`.

    Clamping is iterative because one element hitting its ceiling gives its
    surplus back to the others: a `max_sec` on the middle of three is not a
    reason for the last one to come out short.
    """
    if not elastic:
        return {}

    granted: dict[str, float] = {}
    pending = list(elastic)
    pool = max(0.0, remainder)

    while pending:
        weights = sum(max(0.0, e.duration.grow) for e in pending)
        if weights <= 0:
            # Nobody wants a share; split it evenly rather than dropping it.
            even = pool / len(pending)
            granted.update({e.id: round(_clamp(e, even), 3) for e in pending})
            break

        settled = []
        for element in pending:
            share = pool * max(0.0, element.duration.grow) / weights
            clamped = _clamp(element, share)
            if abs(clamped - share) > 1e-9:
                # A bound bit, so this element's length is now fixed and the
                # others divide what is left of the pool again.
                granted[element.id] = round(clamped, 3)
                settled.append(element)
        if not settled:
            granted.update({
                e.id: round(pool * max(0.0, e.duration.grow) / weights, 3)
                for e in pending
            })
            break
        for element in settled:
            pending.remove(element)
            pool -= granted[element.id]
        pool = max(0.0, pool)

    return granted
