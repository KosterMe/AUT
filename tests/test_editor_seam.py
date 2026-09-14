"""The editor and the model agree on the names of things.

There is a second seam in this system, and it is quieter than the one between
AUT and the montage package: the editor is TypeScript and the model is Python,
so nothing the compiler on either side does can notice that the two lists of
property names have drifted apart. Three bugs have come out of exactly this —
a dict of two names where there were five (trap 53), a property added to the
model and not to the editor, and a row edited by its position rather than by
its identity (trap 56).

So the lists are compared here, by reading the TypeScript as text. That is
crude and it is the point: a regular expression over a source file costs
nothing, needs no Node, and answers the only question worth asking — are these
the same six names?

The editor's own logic is tested where it lives, by `pnpm test` in `webapp/`.
This file is about the seam and nothing else.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from montage import composition as comp
from montage.render import capabilities as caps
from montage.scenario import model as scenario_model

WEBAPP = Path(__file__).resolve().parents[1] / "webapp" / "src"
MODEL = WEBAPP / "pages" / "scenarioModel.ts"


def names_in(source: str, constant: str) -> tuple[str, ...]:
    """The string literals of a `const X = [...] as const` declaration."""
    match = re.search(rf"(?:export )?const {constant} = \[(.*?)\] as const;", source, re.S)
    assert match, f"{constant} is not declared the way this test reads it"
    return tuple(re.findall(r'"([^"]+)"', match.group(1)))


def keys_of(source: str, constant: str) -> set[str]:
    """The keys of a `Record`-style object literal, by their name before the colon."""
    # `export` is optional: `RESTING` is private to the module on purpose,
    # and being private is no reason to let it drift.
    match = re.search(rf"(?:export )?const {constant}[^=]*= \{{(.*?)\n\}};", source, re.S)
    assert match, f"{constant} is not declared the way this test reads it"
    return set(re.findall(r"^\s{2}(\w+):", match.group(1), re.M))


@pytest.fixture(scope="module")
def model() -> str:
    assert MODEL.exists(), f"the editor's model has moved: {MODEL}"
    return MODEL.read_text(encoding="utf-8")


def test_the_editor_animates_exactly_what_the_composition_can_carry(model: str):
    """`ANIMATABLE` and `composition.CURVES` are the same list in two
    languages. A property in one and not the other is silent both ways: added
    to the model only, it is never offered; added to the editor only, it is
    offered and then dropped on compile."""
    assert names_in(model, "ANIMATABLE") == comp.CURVES


def test_the_probe_speaks_of_the_same_properties(model: str):
    """The editor reads `animatable` from the probe's payload by these names
    (§7.2). A name that matched nothing there would read as "this build cannot
    do it", and the property would vanish from a perfectly capable ffmpeg."""
    assert set(names_in(model, "ANIMATABLE")) == set(caps.ANIMATES)


@pytest.mark.parametrize("constant", ["PROPERTY_LABELS", "RESTING", "SCALES"])
def test_every_animated_property_is_described_by_every_table(model: str, constant: str):
    """Each of these is a name written out again, and each is silent when it is
    missing one: no label, no resting value, no step. The editor's own suite
    checks the same thing from the inside; this checks it against the model,
    which is where the list is actually decided."""
    assert keys_of(model, constant) == set(comp.CURVES)


def test_the_editor_labels_every_slot_the_model_has(model: str):
    """The dropdown is built from `SLOT_LABELS`, and a kind missing from it
    would be unselectable — including for a scenario that already uses it,
    which could then not be opened without silently becoming something else."""
    from montage.scenario import model as scenario_model

    assert keys_of(model, "SLOT_LABELS") == set(scenario_model.SLOT_KINDS)


@pytest.mark.parametrize("filename", ["pages/Scenarios.tsx", "components/ScenarioProperties.tsx"])
def test_every_place_that_offers_a_slot_asks_which_ones_are_drawn(filename: str):
    """Which slots this montage draws is the compiler's answer, served over
    `/api/capabilities` (trap 59). The two places that offer a slot — the
    palette and the "filled with" dropdown — have to read it rather than
    decide for themselves, or they go stale the moment a slot is implemented.

    Checked as a positive: slot *names* belong in the editor, that is the
    model's vocabulary, so their presence proves nothing. What proves
    something is asking.
    """
    source = (WEBAPP / filename).read_text(encoding="utf-8")

    assert "slots?.[" in source, f"{filename} offers a slot without asking"
    assert "useCapabilities" in source


def test_the_editor_does_not_carry_its_own_map_of_probe_constructions():
    """Which construction carries which property is `ANIMATES`, and it lives
    beside the renderer that picks those constructions. If a copy of it grew in
    the editor the two would drift, and the editor would offer a track for a
    filter this build does not have (trap 53 in a new coat)."""
    constructions = {key for keys in caps.ANIMATES.values() for key in keys}

    for path in sorted(WEBAPP.rglob("*.ts*")):
        if path.name.endswith(".test.ts"):
            continue
        source = path.read_text(encoding="utf-8")
        found = {name for name in constructions if f'"{name}"' in source}
        assert not found, f"{path.name} names probe constructions: {found}"
