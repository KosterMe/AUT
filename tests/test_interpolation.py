"""The interpolation specification, as numbers somebody can argue with.

§11 asks for one specification, two implementations and one set of vectors —
the editor's arithmetic drifting from the renderer's by a decimal place is the
kind of bug nobody sees until a clip is wrong. There is one implementation
today (`montage/scenario/curve.py`; the editor samples the server rather than
repeating the maths — §8.5), so the vectors are what a second one would have
to satisfy, and they are written down here rather than read off the code:
every number below was worked out from the spec, not recorded from a run.

`tests/golden/interpolation.json` is the same table as data, for an
implementation that cannot import Python.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from montage.scenario import curve, facts as fact_module, model

VECTORS = Path(__file__).parent / "golden" / "interpolation.json"

# at, value, easing — and what the curve must be worth at each sample.
CASES: list[dict] = [
    {
        "name": "linear",
        "keys": [(0.0, 0.0, "linear"), (2.0, 100.0, "linear")],
        "samples": [(-1.0, 0.0), (0.0, 0.0), (0.5, 25.0), (1.0, 50.0), (2.0, 100.0),
                    (3.0, 100.0)],
        "why": "a straight line, and held at both ends rather than extrapolated",
    },
    {
        "name": "ease_in",
        "keys": [(0.0, 0.0, "in"), (2.0, 100.0, "linear")],
        # p², sampled where the polyline has its points: 1/4 of the way in is
        # a sixteenth of the distance.
        "samples": [(0.5, 6.25), (1.0, 25.0), (1.5, 56.25), (2.0, 100.0)],
        "why": "slow away from the key it leaves",
    },
    {
        "name": "ease_out",
        "keys": [(0.0, 0.0, "out"), (2.0, 100.0, "linear")],
        "samples": [(0.5, 43.75), (1.0, 75.0), (1.5, 93.75), (2.0, 100.0)],
        "why": "fast away, slow into the key it arrives at",
    },
    {
        "name": "ease_in_out",
        "keys": [(0.0, 0.0, "in_out"), (2.0, 100.0, "linear")],
        "samples": [(0.5, 12.5), (1.0, 50.0), (1.5, 87.5), (2.0, 100.0)],
        "why": "symmetric, and exactly half way at half way",
    },
    {
        "name": "step",
        "keys": [(0.0, 10.0, "step"), (2.0, 90.0, "linear")],
        "samples": [(0.0, 10.0), (1.0, 10.0), (1.999, 10.0), (2.0, 90.0), (3.0, 90.0)],
        "why": "holds and jumps: no value in between was ever asked for",
    },
    {
        "name": "one_key_is_a_constant",
        "keys": [(1.0, 42.0, "linear")],
        "samples": [(0.0, 42.0), (1.0, 42.0), (9.0, 42.0)],
        "why": "nothing to move between, so nothing moves",
    },
    {
        "name": "three_keys_each_with_its_own_shape",
        "keys": [(0.0, 0.0, "linear"), (1.0, 50.0, "step"), (2.0, 100.0, "linear")],
        "samples": [(0.5, 25.0), (1.0, 50.0), (1.5, 50.0), (2.0, 100.0)],
        "why": "easing belongs to the key it leaves, so only the middle stretch holds",
    },
    {
        "name": "two_keys_on_one_second_are_a_jump",
        "keys": [(0.0, 0.0, "linear"), (1.0, 30.0, "linear"), (1.0, 70.0, "linear"),
                 (2.0, 70.0, "linear")],
        "samples": [(0.5, 15.0), (1.0, 70.0), (1.5, 70.0)],
        "why": "an editor can produce it with anchors; the later key wins",
    },
]


def animated(keys: list[tuple[float, float, str]]) -> model.Animated:
    return model.Animated(0.0, keys=tuple(
        model.Keyframe(
            at=model.Anchor(mode=model.AnchorMode.START, value=at),
            value=value,
            easing=easing,
        )
        for at, value, easing in keys
    ))


def built(keys: list[tuple[float, float, str]]) -> tuple[tuple[float, float], ...]:
    return curve.points(
        animated(keys), clip_duration_sec=10.0, facts=fact_module.ClipFacts(end_sec=10.0),
    )


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_curve_is_worth_what_the_specification_says(case):
    points = built(case["keys"])

    for at, expected in case["samples"]:
        assert curve.value_at(points, at) == pytest.approx(expected, abs=1e-6), (
            f"{case['name']} at {at}s — {case['why']}"
        )


def test_a_value_with_no_keys_does_not_move():
    """And that has to stay cheap: a scenario without animation must not pay
    for animation it does not have (§4.3)."""
    assert curve.points(
        model.Animated(50.0), clip_duration_sec=10.0, facts=fact_module.ClipFacts(end_sec=10.0),
    ) == ()
    assert curve.value_at((), 3.0, static=50.0) == 50.0


def test_keys_are_anchored_rather_than_timed():
    """The reason a keyframe carries an anchor at all: the same scenario on a
    clip of another length still leaves the frame half a second before the
    end, instead of at second 9.5 of a six-second clip."""
    leaving = model.Animated(0.0, keys=(
        model.Keyframe(at=model.Anchor(), value=0.0),
        model.Keyframe(
            at=model.Anchor(mode=model.AnchorMode.END, offset_sec=-0.5), value=100.0,
        ),
    ))

    short = curve.points(
        leaving, clip_duration_sec=6.0, facts=fact_module.ClipFacts(end_sec=6.0))
    long = curve.points(
        leaving, clip_duration_sec=30.0, facts=fact_module.ClipFacts(end_sec=30.0))

    assert short[-1] == (5.5, 100.0)
    assert long[-1] == (29.5, 100.0)


def test_an_unknown_easing_is_a_straight_line_rather_than_a_failure():
    """A scenario written by a newer version still lays out, and the least
    surprising thing it can do between two keys is go straight there."""
    points = built([(0.0, 0.0, "пружина"), (2.0, 100.0, "linear")])

    assert curve.value_at(points, 1.0) == 50.0


def test_the_vectors_on_disk_are_the_same_table():
    """Written down as data so a second implementation — the editor's, if it
    ever stops asking the server — can be held to the same numbers without
    importing Python."""
    expected = {
        "spec": "montage/scenario/curve.py",
        "note": (
            "Samples are of the polyline the renderer is given, easing already "
            "applied. at/value/easing per key; samples are [second, value]."
        ),
        "cases": [
            {
                "name": case["name"],
                "why": case["why"],
                "keys": [
                    {"at": at, "value": value, "easing": easing}
                    for at, value, easing in case["keys"]
                ],
                "samples": [[at, value] for at, value in case["samples"]],
            }
            for case in CASES
        ],
    }

    if not VECTORS.exists():  # pragma: no cover - only on a new vectors file
        VECTORS.write_text(
            json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        pytest.fail(f"wrote {VECTORS}; check it in and re-run")

    assert json.loads(VECTORS.read_text(encoding="utf-8")) == expected
