"""Laying out the spine, and turning anchors into seconds.

Two pure steps, tested by value. What matters most is the short clip: a
scenario written against 90 seconds of material meeting 6 seconds of it is
normal, not exceptional, and what it does then is a policy rather than an
accident.
"""
from __future__ import annotations

import pytest

from montage import scenario as sc
from montage.scenario import anchors, layout
from montage.subtitles import SubtitleCue


def piece(name, *, optional=False, priority=0, **duration) -> sc.Element:
    return sc.Element(
        id=name, optional=optional, priority=priority, duration=sc.Duration(**duration)
    )


def spans(result) -> list[tuple[str, float, float]]:
    return [(p.element.id, p.start_sec, p.duration_sec) for p in result.placements]


class TestTheSpineIsAFlexbox:
    def test_one_elastic_element_takes_the_whole_clip(self):
        """The shape every clip has today: one segment, end to end."""
        result = layout.lay_out((piece("src", mode=sc.DurationMode.ELASTIC),), available_sec=90.0)

        assert spans(result) == [("src", 0.0, 90.0)]
        assert result.duration_sec == 90.0

    def test_fixed_elements_take_theirs_and_the_elastic_one_takes_the_rest(self):
        result = layout.lay_out((
            piece("hook", mode=sc.DurationMode.FIXED, value=3.0),
            piece("body", mode=sc.DurationMode.ELASTIC),
            piece("outro", mode=sc.DurationMode.FIXED, value=5.0),
        ), available_sec=90.0)

        assert spans(result) == [("hook", 0.0, 3.0), ("body", 3.0, 82.0), ("outro", 85.0, 5.0)]

    def test_two_elastic_elements_share_by_grow(self):
        result = layout.lay_out((
            piece("a", mode=sc.DurationMode.ELASTIC, grow=1.0),
            piece("b", mode=sc.DurationMode.ELASTIC, grow=3.0),
        ), available_sec=100.0)

        assert spans(result) == [("a", 0.0, 25.0), ("b", 25.0, 75.0)]

    def test_a_fraction_is_a_share_of_the_clip(self):
        result = layout.lay_out((
            piece("half", mode=sc.DurationMode.FRACTION, value=0.5),
            piece("rest", mode=sc.DurationMode.ELASTIC),
        ), available_sec=60.0)

        assert spans(result) == [("half", 0.0, 30.0), ("rest", 30.0, 30.0)]

    def test_a_ceiling_hands_its_surplus_to_the_others(self):
        """Otherwise a max on the middle of three shortens the last one."""
        result = layout.lay_out((
            piece("capped", mode=sc.DurationMode.ELASTIC, max_sec=10.0),
            piece("rest", mode=sc.DurationMode.ELASTIC),
        ), available_sec=100.0)

        assert spans(result) == [("capped", 0.0, 10.0), ("rest", 10.0, 90.0)]

    def test_a_floor_is_honoured_over_a_fair_share(self):
        result = layout.lay_out((
            piece("a", mode=sc.DurationMode.ELASTIC, grow=1.0, min_sec=20.0),
            piece("b", mode=sc.DurationMode.ELASTIC, grow=9.0),
        ), available_sec=40.0)

        assert spans(result)[0] == ("a", 0.0, 20.0)

    def test_a_natural_length_comes_from_the_material(self):
        result = layout.lay_out(
            (piece("asset", mode=sc.DurationMode.NATURAL),
             piece("rest", mode=sc.DurationMode.ELASTIC)),
            available_sec=60.0,
            natural_lookup=lambda element: 8.0,
        )

        assert spans(result) == [("asset", 0.0, 8.0), ("rest", 8.0, 52.0)]

    def test_a_natural_length_nobody_knows_stretches_rather_than_vanishing(self):
        result = layout.lay_out(
            (piece("asset", mode=sc.DurationMode.NATURAL),),
            available_sec=60.0, natural_lookup=lambda element: None,
        )

        assert spans(result) == [("asset", 0.0, 60.0)]


class TestAClipTooShortForItsScenario:
    """Normal material, not an error. The policy, in its three steps."""

    def test_first_the_elastic_ones_shrink(self):
        result = layout.lay_out((
            piece("hook", mode=sc.DurationMode.FIXED, value=3.0),
            piece("body", mode=sc.DurationMode.ELASTIC),
        ), available_sec=6.0)

        assert spans(result) == [("hook", 0.0, 3.0), ("body", 3.0, 3.0)]
        assert result.dropped == ()

    def test_then_the_expendable_ones_are_dropped(self):
        result = layout.lay_out((
            piece("hook", mode=sc.DurationMode.FIXED, value=3.0),
            piece("body", mode=sc.DurationMode.ELASTIC, min_sec=2.0),
            piece("outro", optional=True, mode=sc.DurationMode.FIXED, value=5.0),
        ), available_sec=6.0)

        assert result.dropped == ("outro",)
        assert spans(result) == [("hook", 0.0, 3.0), ("body", 3.0, 3.0)]

    def test_the_cheapest_one_goes_first(self):
        result = layout.lay_out((
            piece("body", mode=sc.DurationMode.ELASTIC, min_sec=2.0),
            piece("cheap", optional=True, priority=0, mode=sc.DurationMode.FIXED, value=5.0),
            piece("dear", optional=True, priority=9, mode=sc.DurationMode.FIXED, value=5.0),
        ), available_sec=8.0)

        assert result.dropped == ("cheap",)

    def test_and_then_the_clip_is_truncated_rather_than_lost(self):
        """Losing an outro beats losing the clip, so this warns and renders."""
        result = layout.lay_out((
            piece("hook", mode=sc.DurationMode.FIXED, value=3.0),
            piece("outro", mode=sc.DurationMode.FIXED, value=5.0),
        ), available_sec=6.0)

        assert result.overflow_sec == 2.0
        assert spans(result) == [("hook", 0.0, 3.0), ("outro", 3.0, 3.0)]

    def test_nothing_is_ever_placed_at_zero_length(self):
        """A zero-length segment is one no renderer would accept."""
        result = layout.lay_out((
            piece("all", mode=sc.DurationMode.FIXED, value=6.0),
            piece("none", mode=sc.DurationMode.FIXED, value=4.0),
        ), available_sec=6.0)

        assert [name for name, _, _ in spans(result)] == ["all"]

    def test_an_empty_spine_is_a_clip_of_no_length(self):
        assert layout.lay_out((), available_sec=90.0).duration_sec == 0.0


class TestAnchorsBecomeSeconds:
    def facts(self, **kwargs) -> sc.ClipFacts:
        return sc.ClipFacts(**kwargs)

    def at(self, anchor, *, duration=60.0, facts=None, placed=None):
        return anchors.resolve(
            anchor, clip_duration_sec=duration,
            facts=facts or self.facts(), placed=placed or {},
        )

    def test_start_counts_from_the_beginning(self):
        assert self.at(sc.Anchor(mode=sc.AnchorMode.START, value=3.0))[0] == 3.0

    def test_end_counts_back_from_the_end(self):
        assert self.at(sc.Anchor(mode=sc.AnchorMode.END, value=2.0))[0] == 58.0

    def test_a_fraction_is_a_share_of_the_length(self):
        assert self.at(sc.Anchor(mode=sc.AnchorMode.FRACTION, value=0.5))[0] == 30.0

    def test_the_same_anchors_move_with_the_clip(self):
        """The whole point: three intentions that coincide on one clip and
        come apart on another."""
        modes = (
            sc.Anchor(mode=sc.AnchorMode.START, value=15.0),
            sc.Anchor(mode=sc.AnchorMode.END, value=15.0),
            sc.Anchor(mode=sc.AnchorMode.FRACTION, value=0.5),
        )
        on_thirty = [self.at(a, duration=30.0)[0] for a in modes]
        on_ninety = [self.at(a, duration=90.0)[0] for a in modes]

        assert on_thirty == [15.0, 15.0, 15.0]     # indistinguishable here
        assert on_ninety == [15.0, 75.0, 45.0]     # and quite distinct here

    def test_an_offset_is_added_after_the_mode_is_resolved(self):
        assert self.at(sc.Anchor(mode=sc.AnchorMode.FRACTION, value=0.5, offset_sec=2.0))[0] == 32.0

    def test_an_anchor_outside_the_clip_is_moved_in_and_says_so(self):
        at, note = self.at(sc.Anchor(mode=sc.AnchorMode.END, value=90.0), duration=10.0)

        assert at == 0.0
        assert "outside a" in note

    def test_after_follows_the_element_it_names(self):
        placed = {"intro": anchors.Span(0.0, 4.0)}
        at, _ = self.at(sc.Anchor(mode=sc.AnchorMode.AFTER, ref="intro"), placed=placed)

        assert at == 4.0

    def test_before_precedes_it(self):
        placed = {"outro": anchors.Span(50.0, 5.0)}
        at, _ = self.at(sc.Anchor(mode=sc.AnchorMode.BEFORE, ref="outro"), placed=placed)

        assert at == 50.0

    def test_following_something_that_was_dropped_says_so(self):
        at, note = self.at(sc.Anchor(mode=sc.AnchorMode.AFTER, ref="gone"))

        assert at == 0.0 and "nothing to follow" in note


class TestAnchorsOnTheMaterial:
    def test_a_cut_anchor_lands_on_a_join(self):
        facts = sc.ClipFacts(keep=((0.0, 10.0), (12.0, 20.0)))
        at, _ = anchors.resolve(
            sc.Anchor(mode=sc.AnchorMode.EVENT, event=sc.EventRef(kind="cut")),
            clip_duration_sec=18.0, facts=facts, placed={},
        )

        assert at == 10.0

    def test_a_word_anchor_lands_where_it_is_said(self):
        """In output time: the cues come from the compiler, which has already
        laid the spine out, not from the source-timed transcript."""
        cues = (SubtitleCue(start_sec=4.0, end_sec=5.0, text="про машину"),)
        at, _ = anchors.resolve(
            sc.Anchor(mode=sc.AnchorMode.EVENT,
                      event=sc.EventRef(kind="word", word="машину")),
            clip_duration_sec=60.0, facts=sc.ClipFacts(), placed={}, cues=cues,
        )

        assert at == 4.0

    def test_a_word_nobody_says_moves_to_the_start_and_says_so(self):
        at, note = anchors.resolve(
            sc.Anchor(mode=sc.AnchorMode.EVENT,
                      event=sc.EventRef(kind="word", word="самолёт")),
            clip_duration_sec=60.0, facts=sc.ClipFacts(), placed={},
        )

        assert at == 0.0 and "самолёт" in note

    def test_the_loudest_moment_is_the_loudest_second(self):
        facts = sc.ClipFacts(loudness=((0.0, -30.0), (7.0, -8.0), (12.0, -25.0)))
        at, _ = anchors.resolve(
            sc.Anchor(mode=sc.AnchorMode.EVENT, event=sc.EventRef(kind="loudest")),
            clip_duration_sec=60.0, facts=facts, placed={},
        )

        assert at == 7.0

    def test_asking_for_the_fifth_cut_of_two_uses_the_nearest_and_says_so(self):
        facts = sc.ClipFacts(keep=((0.0, 10.0), (12.0, 20.0), (25.0, 30.0)))
        at, note = anchors.resolve(
            sc.Anchor(mode=sc.AnchorMode.EVENT, event=sc.EventRef(kind="cut", index=4)),
            clip_duration_sec=23.0, facts=facts, placed={},
        )

        assert at == 18.0 and "not 5" in note

    def test_an_event_this_build_cannot_measure_says_so(self):
        at, note = anchors.resolve(
            sc.Anchor(mode=sc.AnchorMode.EVENT, event=sc.EventRef(kind="beat")),
            clip_duration_sec=60.0, facts=sc.ClipFacts(), placed={},
        )

        assert at == 0.0 and "beat" in note



class TestOrderingElementsThatPointAtEachOther:
    def test_an_element_comes_after_what_it_follows(self):
        first = sc.Element(id="a")
        second = sc.Element(id="b", start=sc.Anchor(mode=sc.AnchorMode.AFTER, ref="a"))

        assert [e.id for e in anchors.order((second, first))] == ["a", "b"]

    def test_elements_with_no_references_keep_the_order_they_were_written_in(self):
        written = tuple(sc.Element(id=name) for name in "abcd")

        assert [e.id for e in anchors.order(written)] == list("abcd")

    def test_a_chain_is_walked_end_to_end(self):
        a = sc.Element(id="a")
        b = sc.Element(id="b", start=sc.Anchor(mode=sc.AnchorMode.AFTER, ref="a"))
        c = sc.Element(id="c", start=sc.Anchor(mode=sc.AnchorMode.AFTER, ref="b"))

        assert [e.id for e in anchors.order((c, b, a))] == ["a", "b", "c"]

    def test_a_loop_is_refused_by_name_rather_than_hung_on(self):
        """Saveable by accident in an editor. The difference between refusing
        it here and refusing it in the renderer is a message naming two
        elements versus a worker that never returns."""
        x = sc.Element(id="x", start=sc.Anchor(mode=sc.AnchorMode.AFTER, ref="y"))
        y = sc.Element(id="y", start=sc.Anchor(mode=sc.AnchorMode.BEFORE, ref="x"))

        with pytest.raises(anchors.CycleError, match="x, y"):
            anchors.order((x, y))

    def test_a_blur_of_itself_is_the_same_loop_spelled_differently(self):
        narcissus = sc.Element(id="a", slot=sc.Slot(kind=sc.SLOT_BLUR_OF, ref="b"))
        other = sc.Element(id="b", slot=sc.Slot(kind=sc.SLOT_BLUR_OF, ref="a"))

        with pytest.raises(anchors.CycleError):
            anchors.order((narcissus, other))
