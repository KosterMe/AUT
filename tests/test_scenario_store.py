"""A scenario stored as data, and read back as the same scenario.

This is the only file where a scenario crosses out of memory, and it is the
seam where the failure mode is silence: a field that is written but not read
comes back as its default, the render succeeds, and the clip is simply not the
montage somebody built. So the load-bearing test here is equality of the whole
object for every built-in — not a spot check of a few fields.
"""
from __future__ import annotations

import json

import pytest

from montage.scenario import builtin, model, store
from montage.style import StyleSpec


def style() -> StyleSpec:
    return StyleSpec.from_settings()


class TestEveryBuiltinSurvivesTheRoundTrip:
    """Through JSON, because the column is text and not a dict."""

    @pytest.mark.parametrize("name", sorted(builtin.BUILTIN))
    def test_it_comes_back_equal(self, name):
        original = builtin.for_profile(name, style())
        stored = json.loads(json.dumps(store.to_dict(original)))

        assert store.from_dict(stored) == original

    @pytest.mark.parametrize("name", sorted(builtin.BUILTIN))
    def test_and_the_style_with_it(self, name):
        """The scenario carries a look, and it is the half most easily lost:
        the compiler reads it for everything the tracks do not say."""
        original = builtin.for_profile(name, style())

        assert store.from_dict(store.to_dict(original)).style == original.style

    def test_a_rule_is_still_a_rule_and_not_an_element(self):
        """They are stored side by side in one list. What tells them apart is
        the `rule` key, because it is the field that makes one a rule — a
        separate type tag is a second truth that can disagree with the first."""
        original = builtin.talking(style())
        overlay = [t for t in original.tracks if t.kind == model.TRACK_OVERLAY][0]
        assert any(isinstance(e, model.RuleElement) for e in overlay.elements)

        back = store.from_dict(store.to_dict(original))
        restored = [t for t in back.tracks if t.kind == model.TRACK_OVERLAY][0]

        assert [type(e) for e in restored.elements] == [type(e) for e in overlay.elements]

    def test_an_animated_field_keeps_its_keyframes(self):
        """Nothing built-in animates yet (§7.2 said what this build can do),
        so this is the one thing the round-trip cannot prove on its own."""
        element = model.Element(
            id="mover",
            frame=model.Frame(x=model.Animated(10.0, keys=(
                model.Keyframe(at=model.Anchor(), value=10.0),
                model.Keyframe(
                    at=model.Anchor(mode=model.AnchorMode.END), value=90.0, easing="ease_out",
                ),
            ))),
        )
        original = model.Scenario(
            name="moving", tracks=(model.Track(id="v", elements=(element,)),),
        )

        back = store.from_dict(json.loads(json.dumps(store.to_dict(original))))

        assert back == original
        assert back.tracks[0].elements[0].frame.x.keys[1].easing == "ease_out"


class TestAHandWrittenScenario:
    """Stored scenarios are editable by hand, so the reader is generous about
    what it takes and exact about what it means."""

    def test_almost_everything_may_be_left_out(self):
        scenario = store.from_dict({"name": "bare"})

        assert scenario.name == "bare"
        assert scenario.tracks == ()
        assert scenario.canvas.width == 1080
        assert scenario.mock.duration_sec == 90.0

    def test_a_bare_number_is_a_static_value(self):
        """`"width": 50` is what somebody writes when they mean a rectangle
        half the canvas wide, and it plainly means that."""
        scenario = store.from_dict({
            "name": "flat",
            "tracks": [{"id": "v", "elements": [{"id": "a", "frame": {"width": 50}}]}],
        })

        assert scenario.tracks[0].elements[0].frame.width.static == 50.0

    def test_an_unknown_field_is_ignored_rather_than_fatal(self):
        """A scenario written by a newer version, opened by an older one. The
        fields it does understand still mean what they say."""
        scenario = store.from_dict({
            "name": "future", "hologram": True,
            "tracks": [{"id": "v", "elements": [{"id": "a", "label": "kept"}]}],
        })

        assert scenario.tracks[0].elements[0].label == "kept"


class TestWhatCannotBeRead:
    """Refused, not guessed. A scenario read as a different montage is a month
    of clips that are quietly not what anybody asked for."""

    def test_a_scenario_is_an_object(self):
        with pytest.raises(store.MalformedScenario):
            store.from_dict(["not", "an", "object"])

    def test_a_track_is_an_object(self):
        with pytest.raises(store.MalformedScenario, match="track"):
            store.from_dict({"name": "x", "tracks": ["spine"]})

    def test_an_element_is_an_object(self):
        with pytest.raises(store.MalformedScenario, match="element"):
            store.from_dict({"name": "x", "tracks": [{"id": "v", "elements": ["a"]}]})

    def test_an_unknown_anchor_mode(self):
        with pytest.raises(store.MalformedScenario, match="anchor mode"):
            store.from_dict({"name": "x", "tracks": [{
                "id": "v", "elements": [{"id": "a", "start": {"mode": "whenever"}}],
            }]})

    def test_an_unknown_duration_mode(self):
        with pytest.raises(store.MalformedScenario, match="duration mode"):
            store.from_dict({"name": "x", "tracks": [{
                "id": "v", "elements": [{"id": "a", "duration": {"mode": "a while"}}],
            }]})

    def test_a_model_rule_broken_by_hand(self):
        """The model's own validation, reported as a storage error: two spines
        is not a reading problem, but it arrives the same way."""
        with pytest.raises(store.MalformedScenario, match="spine"):
            store.from_dict({"name": "x", "tracks": [
                {"id": "a", "kind": "spine"}, {"id": "b", "kind": "spine"},
            ]})
