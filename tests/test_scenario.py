"""The scenario model, and what a scenario says it needs to know.

`required_facts` is the load-bearing one. It decides whether a job pays for
Whisper, and it is *derived* from the scenario rather than configured beside
it — so the tests below are the table in §6.1 of the design, one row at a time.
Getting a row wrong is not a crash: the fact is simply never computed, and the
anchor that wanted it silently resolves to the start of the clip.
"""
from __future__ import annotations

import dataclasses

import pytest

from app.domain import scenario as sc
from app.domain.inserts import AssetOption
from app.domain.style import StyleSpec


def style(**groups) -> StyleSpec:
    """A style with one or two groups changed, everything else default."""
    base = StyleSpec.from_settings()
    for name, fields in groups.items():
        base = dataclasses.replace(
            base, **{name: dataclasses.replace(getattr(base, name), **fields)}
        )
    return base


def scenario_with(*elements, kind=sc.TRACK_OVERLAY, **kwargs) -> sc.Scenario:
    spine = sc.Element(id="src", duration=sc.Duration(mode=sc.DurationMode.ELASTIC))
    tracks = [sc.Track(id="spine", kind=sc.TRACK_SPINE, elements=(spine,))]
    if elements:
        tracks.append(sc.Track(id="over", kind=kind, z=1, elements=tuple(elements)))
    return sc.Scenario(name="t", tracks=tuple(tracks), **kwargs)


class TestModelRefusesNonsense:
    """Caught on save, in an editor, where there is somebody to tell."""

    def test_a_scenario_has_one_spine_not_two(self):
        spine = sc.Track(id="a", kind=sc.TRACK_SPINE)
        with pytest.raises(ValueError, match="exactly one spine"):
            sc.Scenario(name="t", tracks=(spine, dataclasses.replace(spine, id="b")))

    def test_element_ids_are_unique_because_anchors_point_at_them(self):
        with pytest.raises(ValueError, match="unique"):
            sc.Scenario(name="t", tracks=(
                sc.Track(id="a", elements=(sc.Element(id="x"),)),
                sc.Track(id="b", elements=(sc.Element(id="x"),)),
            ))

    def test_a_library_slot_without_a_tag_has_nothing_to_look_for(self):
        with pytest.raises(ValueError, match="tag"):
            sc.Slot(kind=sc.SLOT_LIBRARY)

    def test_a_blur_needs_something_to_be_a_blur_of(self):
        with pytest.raises(ValueError, match="blur"):
            sc.Slot(kind=sc.SLOT_BLUR_OF)

    def test_a_relative_anchor_needs_the_element_it_is_relative_to(self):
        with pytest.raises(ValueError, match="relative to"):
            sc.Anchor(mode=sc.AnchorMode.AFTER)

    def test_a_fraction_anchor_is_a_fraction(self):
        with pytest.raises(ValueError, match="0.0 to 1.0"):
            sc.Anchor(mode=sc.AnchorMode.FRACTION, value=50.0)

    def test_a_ceiling_below_the_floor_is_refused(self):
        with pytest.raises(ValueError, match="below"):
            sc.Duration(min_sec=10.0, max_sec=5.0)


class TestAFrameThatFillsTheCanvas:
    """Which is how a scenario with no layers compiles to today's graph."""

    def test_the_default_frame_fills_it(self):
        assert sc.Frame().fills_canvas

    def test_a_moved_frame_does_not(self):
        assert not sc.Frame(x=sc.Animated(25.0)).fills_canvas

    def test_an_animated_frame_does_not_even_if_it_starts_full(self):
        moving = sc.Animated(50.0, keys=(sc.Keyframe(at=sc.Anchor(), value=10.0),))
        assert not sc.Frame(x=moving).fills_canvas

    def test_a_static_value_knows_it_is_static(self):
        assert sc.Animated(1.0).is_static
        assert not sc.Animated(1.0, keys=(sc.Keyframe(at=sc.Anchor(), value=0.0),)).is_static


class TestWhatAScenarioNeedsToKnow:
    """§6.1, row by row."""

    def test_every_scenario_needs_the_length(self):
        assert sc.FactKind.DURATION in sc.required_facts(scenario_with())

    def test_subtitles_ask_for_word_timings(self):
        wants = sc.required_facts(scenario_with(style=style(subtitles={"enabled": True})))
        assert sc.FactKind.CUES in wants

    def test_a_keyword_broll_rule_asks_for_them_too(self):
        """B-roll is placed on the words that name it."""
        rule = sc.RuleElement(id="r", rule=sc.RULE_KEYWORD_BROLL)
        wants = sc.required_facts(
            scenario_with(rule, style=style(subtitles={"enabled": False}))
        )
        assert sc.FactKind.CUES in wants

    def test_an_anchor_on_a_word_asks_for_them(self):
        at_word = sc.Element(
            id="e", start=sc.Anchor(mode=sc.AnchorMode.EVENT,
                                    event=sc.EventRef(kind="word", word="машина")),
        )
        wants = sc.required_facts(
            scenario_with(at_word, style=style(subtitles={"enabled": False}))
        )
        assert sc.FactKind.CUES in wants

    def test_removing_pauses_asks_for_the_cuts(self):
        wants = sc.required_facts(scenario_with(style=style(pacing={"remove_silence": True})))
        assert sc.FactKind.CUTS in wants

    def test_an_anchor_on_a_cut_asks_for_them(self):
        at_cut = sc.Element(
            id="e", start=sc.Anchor(mode=sc.AnchorMode.EVENT, event=sc.EventRef(kind="cut")),
        )
        assert sc.FactKind.CUTS in sc.required_facts(scenario_with(at_cut))

    def test_the_loudest_moment_asks_for_the_loudness_curve(self):
        loudest = sc.Element(
            id="e", start=sc.Anchor(mode=sc.AnchorMode.EVENT,
                                    event=sc.EventRef(kind="loudest")),
        )
        assert sc.FactKind.LOUDNESS in sc.required_facts(scenario_with(loudest))

    def test_fitting_the_frame_asks_for_the_source_shape(self):
        fitted = sc.Element(id="e", frame=sc.Frame(fit=sc.FIT_CONTAIN))
        assert sc.FactKind.DIMENSIONS in sc.required_facts(scenario_with(fitted))

    def test_a_blur_of_something_asks_for_it(self):
        blur = sc.Element(id="e", slot=sc.Slot(kind=sc.SLOT_BLUR_OF, ref="src"),
                          frame=sc.Frame(fit=sc.FIT_NONE))
        assert sc.FactKind.DIMENSIONS in sc.required_facts(scenario_with(blur))

    def test_a_library_slot_asks_for_the_library(self):
        asset = sc.Element(id="e", slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag="broll"))
        assert sc.FactKind.ASSETS in sc.required_facts(scenario_with(asset))

    def test_a_keyframe_timed_to_a_word_asks_for_words_too(self):
        """The easy one to miss: anchors hide inside keyframes as well."""
        at_word = sc.Anchor(mode=sc.AnchorMode.EVENT, event=sc.EventRef(kind="word", word="x"))
        moving = sc.Animated(50.0, keys=(sc.Keyframe(at=at_word, value=10.0),))
        element = sc.Element(id="e", frame=sc.Frame(x=moving, fit=sc.FIT_NONE))
        wants = sc.required_facts(
            scenario_with(element, style=style(subtitles={"enabled": False}))
        )
        assert sc.FactKind.CUES in wants

    def test_an_anchor_inside_a_rules_template_asks_too(self):
        template = sc.Element(
            id="t", start=sc.Anchor(mode=sc.AnchorMode.EVENT,
                                    event=sc.EventRef(kind="loudest")),
        )
        rule = sc.RuleElement(id="r", rule=sc.RULE_CADENCE, template=template)
        assert sc.FactKind.LOUDNESS in sc.required_facts(scenario_with(rule))


class TestWhatALeanScenarioCosts:
    """The consequences §6.1 exists for."""

    def test_a_scenario_without_subtitles_never_asks_for_whisper(self):
        """The film profile's problem, gone — not fixed, unreproducible."""
        bare = scenario_with(style=style(
            subtitles={"enabled": False}, pacing={"remove_silence": False}
        ))
        assert sc.FactKind.CUES not in sc.required_facts(bare)

    def test_a_scenario_that_keeps_its_pauses_never_runs_silencedetect(self):
        bare = scenario_with(style=style(pacing={"remove_silence": False}))
        assert sc.FactKind.CUTS not in sc.required_facts(bare)

    def test_one_source_element_costs_one_ffprobe_and_nothing_else(self):
        """A scenario of one `source` with a fit is a probe and a render."""
        bare = scenario_with(style=style(
            subtitles={"enabled": False}, pacing={"remove_silence": False},
            audio={"enabled": False},
        ))
        assert sc.required_facts(bare) == frozenset(
            {sc.FactKind.DURATION, sc.FactKind.DIMENSIONS}
        )

    def test_adding_subtitles_switches_transcription_on_by_itself(self):
        """Today that is two independent switches, and they can disagree."""
        off = style(subtitles={"enabled": False}, pacing={"remove_silence": False})
        on = style(subtitles={"enabled": True}, pacing={"remove_silence": False})

        assert sc.required_facts(scenario_with(style=off)) | {sc.FactKind.CUES} == (
            sc.required_facts(scenario_with(style=on))
        )

    def test_the_editor_is_told_the_price_in_words(self):
        wants = sc.required_facts(sc.default())

        assert "тайминги слов (Whisper)" in sc.describes(wants)
        assert "склейки" in sc.describes(wants)


class TestBuyingOnlyWhatWasAsked:
    def test_a_fact_nobody_wants_is_never_computed(self):
        provider = sc.CachingProvider(sources={
            sc.FactKind.DIMENSIONS: lambda: (1920, 1080),
            sc.FactKind.CUES: lambda: pytest.fail("Whisper ran for a scenario without subtitles"),
        })

        sc.facts.resolve(
            frozenset({sc.FactKind.DIMENSIONS}), provider, base=sc.ClipFacts()
        )

        assert provider.computed == frozenset({sc.FactKind.DIMENSIONS})

    def test_a_fact_two_elements_want_is_computed_once(self):
        calls = []
        provider = sc.CachingProvider(sources={
            sc.FactKind.CUES: lambda: calls.append(1) or (),
        })

        provider.get(sc.FactKind.CUES)
        provider.get(sc.FactKind.CUES)

        assert len(calls) == 1

    def test_what_was_bought_lands_on_the_facts(self):
        provider = sc.CachingProvider(sources={
            sc.FactKind.DIMENSIONS: lambda: (1920, 1080),
            sc.FactKind.ASSETS: lambda: (AssetOption(asset_id=1, path="/a.mp4"),),
        })

        filled = sc.facts.resolve(
            frozenset({sc.FactKind.DIMENSIONS, sc.FactKind.ASSETS}),
            provider, base=sc.ClipFacts(source_path="/s.mp4"),
        )

        assert (filled.width, filled.height) == (1920, 1080)
        assert filled.assets[0].path == "/a.mp4"
        assert filled.source_path == "/s.mp4"


class TestCutsAreDerivedFromTheKeptWindows:
    """A flat list of instants cannot say a pause was removed; the gaps can."""

    def test_joins_land_where_the_kept_windows_end(self):
        facts = sc.ClipFacts(keep=((0.0, 10.0), (12.0, 20.0), (25.0, 30.0)))

        assert facts.cuts == (10.0, 18.0)

    def test_one_window_is_no_join_at_all(self):
        assert sc.ClipFacts(keep=((0.0, 30.0),)).cuts == ()

    def test_no_windows_is_no_join_either(self):
        assert sc.ClipFacts().cuts == ()
