"""Keyframes into a curve: one specification, in one place.

Everything that needs to know where a moving value is at some moment goes
through this file — the compiler, so the renderer moves it; the editor, so it
draws what the renderer will do. Two implementations of this arithmetic would
disagree in the third decimal and nobody would notice until a clip looked
wrong, which is the same reason §8.3 refuses a second renderer in the browser.

The specification, so it can be argued with rather than reverse-engineered:

* A keyframe is anchored, not timed. `at` is an `Anchor`, resolved against the
  clip the same way everything else is, so "half a second before the end"
  keeps meaning that on a clip of another length.
* Keys are sorted by the second they resolve to. Two keys on the same second
  is not an error — an editor can produce it with anchors — and the later one
  in the scenario wins.
* **A key's easing describes how the value leaves it.** The stretch between
  key *i* and key *i+1* is shaped by key *i*'s easing; the last key's easing
  shapes nothing.
* Outside the keys the value is held, not extrapolated: the first key's value
  before it, the last key's value after it. A curve that shot off past its
  last key would put a layer somewhere nobody asked for.
* One key is a constant. Zero keys is `static`, and means the value never
  moves — `is_static` has to stay cheap, because a scenario with no animation
  must not pay for animation it does not have (§4.3).

What comes out is a **polyline in output seconds**: easing has already been
applied, so a spring and a hand-drawn wiggle reach the renderer as the same
kind of thing. That is what keeps the EDL a description of what happens rather
than a plan that still has to be interpreted, and it is why the renderer needs
one expression builder instead of one per easing.
"""
from __future__ import annotations

from typing import NamedTuple

from montage.scenario import anchors, facts as fact_module, model

# (second in output time, value)
Point = tuple[float, float]

EASINGS = ("linear", "in", "out", "in_out", "step")

# How many points an eased stretch is cut into. Eight is where the polyline
# stops being visibly angular at a second or two of movement; every point
# costs a nesting level in the filter expression, and trap 10 is about graphs
# nobody can read.
SAMPLES = 8


def ease(name: str, p: float) -> float:
    """`p` from 0 to 1 along the stretch, shaped.

    Unknown names are linear rather than an error: a scenario written by a
    newer version should still lay out, and a straight line between the two
    keys is the least surprising thing it could do.
    """
    p = max(0.0, min(1.0, p))
    if name == "in":
        return p * p
    if name == "out":
        return 1.0 - (1.0 - p) * (1.0 - p)
    if name == "in_out":
        return 2.0 * p * p if p < 0.5 else 1.0 - 2.0 * (1.0 - p) * (1.0 - p)
    if name == "step":
        # Holds until the next key and jumps there. The jump itself is emitted
        # as two points on the same second by `points`.
        return 0.0
    return p


class Resolved(NamedTuple):
    """One key, once it has met a clip."""

    at_sec: float
    value: float
    easing: str
    # Where the key sits in the scenario's own list — which the sort below
    # loses, and which is the only way to say *which key* this is. The editor
    # shows keys in the order they happen and edits them in the order they
    # were written, and those two orders differ the moment a key anchored to
    # the end sits beside one anchored to the start (trap 56).
    index: int


def resolved(
    animated: model.Animated,
    *,
    clip_duration_sec: float,
    facts: fact_module.ClipFacts,
    placed: dict[str, anchors.Span] | None = None,
    cues: tuple = (),
) -> tuple[Resolved, ...]:
    """The keys, resolved against this clip and sorted by when they land.

    Separate from `points` because two different things want it: the renderer
    wants the polyline between the keys, and the editor wants the keys
    themselves, to draw and to drag. Resolving anchors twice — once for each —
    is how an editor comes to show a keyframe at a second the renderer does
    not put it at.
    """
    if animated.is_static:
        return ()
    out = [
        Resolved(
            round(anchors.resolve(
                key.at, clip_duration_sec=clip_duration_sec, facts=facts,
                placed=placed or {}, cues=cues,
            )[0], 3),
            float(key.value),
            key.easing,
            index,
        )
        for index, key in enumerate(animated.keys)
    ]
    # Stable by time, so two keys on the same second keep the order the
    # scenario wrote them in and the later one ends up last — which is the one
    # that wins, because it is the one the curve arrives at.
    out.sort(key=lambda item: item.at_sec)
    return tuple(out)


def polyline(keys: tuple[Resolved, ...]) -> tuple[Point, ...]:
    """Resolved keys as the polyline the renderer is given."""
    if not keys:
        return ()
    if len(keys) == 1:
        return ((keys[0].at_sec, keys[0].value),)

    out: list[Point] = [(keys[0].at_sec, keys[0].value)]
    for first, second in zip(keys, keys[1:]):
        start, from_value, easing = first.at_sec, first.value, first.easing
        end, to_value = second.at_sec, second.value
        span = end - start
        if span <= 0:
            # Two keys on the same second: a jump, which is a real thing to
            # ask for and needs no points in between.
            out.append((end, to_value))
            continue
        if easing == "step":
            out.append((end, from_value))
            out.append((end, to_value))
            continue
        if easing == "linear" or easing not in EASINGS:
            out.append((end, to_value))
            continue
        for index in range(1, SAMPLES + 1):
            p = index / SAMPLES
            out.append((
                round(start + span * p, 3),
                round(from_value + (to_value - from_value) * ease(easing, p), 4),
            ))
    return tuple(out)


def points(
    animated: model.Animated,
    *,
    clip_duration_sec: float,
    facts: fact_module.ClipFacts,
    placed: dict[str, anchors.Span] | None = None,
    cues: tuple = (),
) -> tuple[Point, ...]:
    """The curve this value follows, as a polyline in output seconds.

    Empty when the value does not move, which the caller must treat as "use
    the static value" rather than as "zero".
    """
    return polyline(resolved(
        animated, clip_duration_sec=clip_duration_sec, facts=facts,
        placed=placed, cues=cues,
    ))


def value_at(curve: tuple[Point, ...], at_sec: float, *, static: float = 0.0) -> float:
    """Where a polyline is at that moment. Held at both ends.

    At the instant of a jump — two points on the same second — the later value
    wins, because that is what the renderer does: its expression tests
    `lt(t, …)`, so the moment a stretch ends belongs to the next one. An
    editor that showed the other value would be showing a frame that is never
    rendered.

    The editor asks this to draw one frame; the renderer never does — it hands
    ffmpeg the whole polyline as an expression and lets it evaluate per frame.
    """
    if not curve:
        return static
    if at_sec <= curve[0][0]:
        return curve[0][1]
    if at_sec >= curve[-1][0]:
        return curve[-1][1]

    index = 0
    for position, (start, _) in enumerate(curve):
        if start <= at_sec:
            index = position
    start, from_value = curve[index]
    end, to_value = curve[index + 1]
    if end <= start:
        return to_value
    share = (at_sec - start) / (end - start)
    return from_value + (to_value - from_value) * share
