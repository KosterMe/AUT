"""What the editor is shown, and why it can be trusted.

The rule this file holds: the editor decides nothing. Every number it draws
comes from one compile of the scenario, so a timeline that shows a block at
25 seconds is showing where the renderer put it — not where a second
implementation of the layout rules thinks it goes. §8.3 refuses a browser-side
renderer for the same reason, and this is that refusal one level up.
"""
from __future__ import annotations

import dataclasses

import pytest

from montage.rules.inserts import AssetOption
from montage.scenario import inspect as inspector, mock, model
from montage.style import StyleSpec


def style(**groups) -> StyleSpec:
    base = StyleSpec.from_settings()
    for name, fields in groups.items():
        base = dataclasses.replace(
            base, **{name: dataclasses.replace(getattr(base, name), **fields)}
        )
    return base


def with_intro_and_outro(**kwargs) -> model.Scenario:
    """A spine that cannot always fit: two fixed ends and an elastic middle."""
    spine = model.Track(id="spine", kind=model.TRACK_SPINE, elements=(
        model.Element(
            id="intro", slot=model.Slot(kind=model.SLOT_SOURCE),
            duration=model.Duration(mode=model.DurationMode.FIXED, value=20.0),
            label="интро",
        ),
        model.Element(
            id="source", slot=model.Slot(kind=model.SLOT_SOURCE),
            duration=model.Duration(mode=model.DurationMode.ELASTIC, grow=1.0),
        ),
        model.Element(
            id="outro", slot=model.Slot(kind=model.SLOT_SOURCE),
            duration=model.Duration(mode=model.DurationMode.FIXED, value=20.0),
            label="аутро", optional=True,
        ),
    ))
    over = model.Track(id="over", kind=model.TRACK_OVERLAY, z=2, elements=(
        model.Element(
            id="cta", slot=model.Slot(kind=model.SLOT_LIBRARY, tag="cta"),
            start=model.Anchor(mode=model.AnchorMode.END, offset_sec=-5.0),
            duration=model.Duration(mode=model.DurationMode.FIXED, value=4.0),
            label="CTA",
        ),
    ))
    return model.Scenario(
        name="t", tracks=(spine, over), style=style(**kwargs) if kwargs else style(),
    )


def report(scenario: model.Scenario, duration_sec: float, assets=()):
    return inspector.inspect(
        scenario, mock.facts(scenario, duration_sec, assets=tuple(assets))
    )


def block(made, element_id: str) -> inspector.Block:
    return next(item for item in made.blocks if item.element_id == element_id)


class TestTheLayoutSwitcher:
    """The editor's most important control (§8.2): the same scenario on
    material it has never been tried on."""

    def test_the_elastic_middle_takes_what_is_left(self):
        short = report(with_intro_and_outro(), 60.0)
        long = report(with_intro_and_outro(), 180.0)

        assert block(short, "source").duration_sec == 20.0   # 60 − 20 − 20
        assert block(long, "source").duration_sec == 140.0

    def test_a_fixed_end_that_does_not_fit_is_shown_as_dropped(self):
        """The mistake the switcher exists to catch: an outro that quietly
        stops existing on a short clip. It is a block that says so, not an
        absence — an absence is what a scenario looks like when it is fine."""
        made = report(with_intro_and_outro(), 30.0)

        outro = block(made, "outro")
        assert outro.placed is False
        assert "too short" in outro.note or "не поместил" in outro.note
        assert [w.code for w in made.warnings if w.element_id == "outro"] == ["dropped"]

    def test_an_element_pinned_to_the_end_moves_with_the_end(self):
        """And is reported as pinned, so the timeline can draw it held to the
        right edge rather than sitting at some number that looks arbitrary."""
        made = report(with_intro_and_outro(), 120.0)

        cta = block(made, "cta")
        assert cta.at_sec == 115.0
        assert cta.anchor == "end"

    def test_the_three_lengths_agree_on_a_scenario_that_renders_whole(self):
        made = report(with_intro_and_outro(), 90.0)

        assert made.material_sec == 90.0
        assert made.timeline_sec == 90.0
        assert made.duration_sec == 90.0

    def test_and_disagree_when_something_cannot_be_rendered_yet(self):
        """An element the renderer cannot make takes its place on the timeline
        and contributes nothing to the file. An editor shown only the file's
        length would draw a timeline that does not match its own blocks.

        A gradient is the example because it is what is still missing: colour
        and text used to be here too, and both are drawn now — the second
        renderer §7.3 reserved for them turned out not to be needed (trap 59).
        """
        scenario = with_intro_and_outro()
        spine = scenario.tracks[0]
        painted = dataclasses.replace(
            spine.elements[0],
            slot=model.Slot(kind=model.SLOT_GRADIENT, color="#000"),
        )
        scenario = dataclasses.replace(scenario, tracks=(
            dataclasses.replace(spine, elements=(painted,) + spine.elements[1:]),
        ) + scenario.tracks[1:])

        made = report(scenario, 90.0)

        assert made.timeline_sec == 90.0
        assert made.duration_sec == 70.0
        assert any(w.code == "unsupported_slot" for w in made.warnings)
        assert block(made, "intro").note

    def test_a_colour_on_the_spine_is_refused_rather_than_dropped(self):
        """It is drawn, not played, and the spine is pieces of material. The
        distinction matters now that a colour renders on a layer: "this slot
        is not supported" and "this slot is not supported *here*" are
        different sentences, and the second one is the true one."""
        scenario = with_intro_and_outro()
        spine = scenario.tracks[0]
        painted = dataclasses.replace(
            spine.elements[0], slot=model.Slot(kind=model.SLOT_COLOR, color="#000")
        )
        scenario = dataclasses.replace(scenario, tracks=(
            dataclasses.replace(spine, elements=(painted,) + spine.elements[1:]),
        ) + scenario.tracks[1:])

        made = report(scenario, 90.0)

        assert any(w.code == "drawn_not_played" for w in made.warnings)
        assert made.duration_sec == 70.0


class TestWhatABlockSays:
    def test_a_block_carries_its_slot_so_the_canvas_can_colour_it(self):
        made = report(with_intro_and_outro(), 90.0)

        cta = block(made, "cta")
        assert (cta.slot_kind, cta.slot_tag) == (model.SLOT_LIBRARY, "cta")
        assert cta.label == "CTA"
        assert cta.track_kind == model.TRACK_OVERLAY

    def test_the_spine_is_drawn_as_the_layout_it_becomes(self):
        """A spine nobody has dragged anything in carries the default
        rectangle, and that does not mean "middle, full size" — it means the
        layout decides, which is how `auto` still meets the shape of the
        source."""
        bottom = (AssetOption(9, "/m/bg/loop.mp4", ("background",), 60.0),)
        made = report(with_intro_and_outro(framing={"layout": "split"}), 90.0, assets=bottom)

        assert made.layout == "split"
        assert block(made, "source").frame.height == 50.0

    def test_a_split_with_nothing_to_put_under_it_is_shown_falling_back(self):
        """Not a failure and not a surprise on the rendered file: the editor
        shows the blurred backdrop it will actually get, and the reason."""
        made = report(with_intro_and_outro(framing={"layout": "split"}), 90.0)

        assert made.layout == "blur"
        assert block(made, "source").frame.height == 100.0
        assert [w.code for w in made.warnings if w.code == "no_companion"] == ["no_companion"]

    def test_a_spine_with_a_rectangle_of_its_own_keeps_it(self):
        """Trap 32, closed. The rectangle used to be collapsed into one of
        three layouts, so an editor that let somebody drag the spine would
        have drawn one thing and rendered another."""
        scenario = with_intro_and_outro()
        spine = scenario.tracks[0]
        upper = dataclasses.replace(
            spine.elements[1],
            frame=model.Frame(
                y=model.Animated(30.0),
                width=model.Animated(100.0),
                height=model.Animated(60.0),
            ),
        )
        scenario = dataclasses.replace(scenario, tracks=(
            dataclasses.replace(spine, elements=(
                spine.elements[0], upper, spine.elements[2],
            )),
        ) + scenario.tracks[1:])

        made = report(scenario, 90.0)
        frame = block(made, "source").frame

        assert (frame.y, frame.height) == (30.0, 60.0)
        # The layout still decides what fills the rest of the canvas.
        assert made.layout in {"blur", "fill"}

    def test_the_spine_still_cannot_move(self):
        """Not for lack of a rectangle: a segment is framed before the join,
        where every segment's clock starts again, so an expression there would
        animate each one identically from its own zero."""
        scenario = with_intro_and_outro()
        spine = scenario.tracks[0]
        moving = dataclasses.replace(
            spine.elements[1],
            frame=model.Frame(x=model.Animated(50.0, keys=(
                model.Keyframe(at=model.Anchor(), value=20.0),
                model.Keyframe(
                    at=model.Anchor(mode=model.AnchorMode.END), value=80.0,
                ),
            ))),
        )
        scenario = dataclasses.replace(scenario, tracks=(
            dataclasses.replace(spine, elements=(
                spine.elements[0], moving, spine.elements[2],
            )),
        ) + scenario.tracks[1:])

        made = report(scenario, 90.0)

        assert any(w.code == "no_spine_animation" for w in made.warnings)
        assert block(made, "source").frame.moving is False

    def test_an_overlay_keeps_its_own_rectangle(self):
        scenario = with_intro_and_outro()
        over = scenario.tracks[1]
        cornered = dataclasses.replace(
            over.elements[0],
            frame=model.Frame(
                x=model.Animated(75.0), y=model.Animated(20.0),
                width=model.Animated(40.0), height=model.Animated(25.0),
            ),
        )
        scenario = dataclasses.replace(scenario, tracks=(
            scenario.tracks[0], dataclasses.replace(over, elements=(cornered,)),
        ))

        frame = block(report(scenario, 90.0), "cta").frame

        assert (frame.x, frame.y, frame.width, frame.height) == (75.0, 20.0, 40.0, 25.0)

    def test_a_resolved_overlay_reports_the_box_that_was_asked_for(self):
        """The EDL flattens a height smaller than the canvas to zero — "the
        aspect ratio decides" — which is true of the render and useless to a
        canvas, which has no picture to take the ratio of."""
        scenario = with_intro_and_outro()
        over = scenario.tracks[1]
        boxed = dataclasses.replace(
            over.elements[0],
            frame=model.Frame(
                x=model.Animated(75.0), y=model.Animated(20.0),
                width=model.Animated(40.0), height=model.Animated(25.0),
            ),
        )
        scenario = dataclasses.replace(scenario, tracks=(
            scenario.tracks[0], dataclasses.replace(over, elements=(boxed,)),
        ))
        library = (AssetOption(3, "/m/cta.mp4", ("cta",), 4.0),)

        frame = block(report(scenario, 90.0, assets=library), "cta").frame

        assert (frame.width, frame.height) == (40.0, 25.0)
        assert frame.moving is False

    def test_a_moving_overlay_is_reported_where_it_is_at_that_moment(self):
        """A canvas showing a moving element where it starts, at every point
        of the timeline, shows a frame that exists for one instant."""
        scenario = with_intro_and_outro()
        over = scenario.tracks[1]
        sliding = dataclasses.replace(
            over.elements[0],
            start=model.Anchor(mode=model.AnchorMode.START, value=0.0),
            duration=model.Duration(mode=model.DurationMode.FIXED, value=10.0),
            frame=model.Frame(x=model.Animated(0.0, keys=(
                model.Keyframe(at=model.Anchor(mode=model.AnchorMode.START, value=0.0),
                               value=0.0),
                model.Keyframe(at=model.Anchor(mode=model.AnchorMode.START, value=10.0),
                               value=100.0),
            ))),
        )
        scenario = dataclasses.replace(scenario, tracks=(
            scenario.tracks[0], dataclasses.replace(over, elements=(sliding,)),
        ))
        library = (AssetOption(3, "/m/cta.mp4", ("cta",), 4.0),)
        facts = mock.facts(scenario, 90.0, assets=library)

        at_start = inspector.inspect(scenario, facts, at_sec=0.0)
        halfway = inspector.inspect(scenario, facts, at_sec=5.0)

        assert block(at_start, "cta").frame.x == 0.0
        assert block(halfway, "cta").frame.x == 50.0
        assert block(halfway, "cta").frame.moving is True

    def test_a_fading_overlay_is_reported_half_faded_halfway_through(self):
        """The canvas draws the block at the opacity this says, so an element
        fading in reads as fading rather than as solid until the render
        disagrees."""
        scenario = with_intro_and_outro()
        over = scenario.tracks[1]
        fading = dataclasses.replace(
            over.elements[0],
            start=model.Anchor(mode=model.AnchorMode.START, value=0.0),
            duration=model.Duration(mode=model.DurationMode.FIXED, value=10.0),
            frame=model.Frame(opacity=model.Animated(1.0, keys=(
                model.Keyframe(at=model.Anchor(mode=model.AnchorMode.START, value=0.0),
                               value=0.0),
                model.Keyframe(at=model.Anchor(mode=model.AnchorMode.START, value=10.0),
                               value=1.0),
            ))),
        )
        scenario = dataclasses.replace(scenario, tracks=(
            scenario.tracks[0], dataclasses.replace(over, elements=(fading,)),
        ))
        library = (AssetOption(3, "/m/cta.mp4", ("cta",), 4.0),)
        facts = mock.facts(scenario, 90.0, assets=library)

        at_start = inspector.inspect(scenario, facts, at_sec=0.0)
        halfway = inspector.inspect(scenario, facts, at_sec=5.0)

        assert block(at_start, "cta").frame.opacity == 0.0
        assert block(halfway, "cta").frame.opacity == 0.5
        # A fade is not a move, but it is still one frame of something that
        # changes, which is what this flag tells the editor.
        assert block(halfway, "cta").frame.moving is True

    def test_every_property_can_carry_a_key_without_breaking_the_screen(self):
        """The editor offers a keyframe track per property and two presets
        that write width and rotate. This function used to look the property
        up in a hand-written dict of "x" and "y", so pressing either preset
        answered the screen that drew it with a 500 (trap 53)."""
        scenario = with_intro_and_outro()
        over = scenario.tracks[1]
        library = (AssetOption(3, "/m/cta.mp4", ("cta",), 4.0),)

        for name in ("x", "y", "width", "height", "rotate", "opacity"):
            resting = {"width": 40.0, "height": 25.0, "opacity": 1.0}.get(name, 10.0)
            animated = dataclasses.replace(
                over.elements[0],
                start=model.Anchor(mode=model.AnchorMode.START, value=0.0),
                duration=model.Duration(mode=model.DurationMode.FIXED, value=10.0),
                frame=model.Frame(**{name: model.Animated(resting, keys=(
                    model.Keyframe(at=model.Anchor(mode=model.AnchorMode.START,
                                                   value=0.0), value=resting),
                    model.Keyframe(at=model.Anchor(mode=model.AnchorMode.START,
                                                   value=10.0), value=resting / 2),
                ))}),
            )
            one = dataclasses.replace(scenario, tracks=(
                scenario.tracks[0], dataclasses.replace(over, elements=(animated,)),
            ))
            got = inspector.inspect(one, mock.facts(one, 90.0, assets=library), at_sec=5.0)

            assert [key.property for key in block(got, "cta").keys] == [name, name], name

    def test_every_key_says_which_key_of_the_scenario_it_is(self):
        """Rows are shown in the order the keys happen; the editor edits them
        in the order they were written. Those differ the moment a key pinned
        to the end sits beside one anchored to the start — and the editor was
        addressing them by their row, so typing a value into the row at 0:03
        changed the key pinned to the end instead (trap 56)."""
        scenario = with_intro_and_outro()
        over = scenario.tracks[1]
        written = (
            model.Keyframe(at=model.Anchor(mode=model.AnchorMode.START, value=0.0),
                           value=10.0),
            model.Keyframe(at=model.Anchor(mode=model.AnchorMode.END, offset_sec=0.0),
                           value=90.0),
            model.Keyframe(at=model.Anchor(mode=model.AnchorMode.START, value=3.0),
                           value=50.0),
        )
        keyed = dataclasses.replace(
            over.elements[0],
            start=model.Anchor(mode=model.AnchorMode.START, value=0.0),
            duration=model.Duration(mode=model.DurationMode.FIXED, value=10.0),
            frame=model.Frame(x=model.Animated(10.0, keys=written)),
        )
        one = dataclasses.replace(scenario, tracks=(
            scenario.tracks[0], dataclasses.replace(over, elements=(keyed,)),
        ))
        library = (AssetOption(3, "/m/cta.mp4", ("cta",), 4.0),)

        keys = block(report(one, 90.0, assets=library), "cta").keys

        # Reported in the order they happen: 0, 3, then the one at the end.
        assert [key.at_sec for key in keys] == [0.0, 3.0, 90.0]
        # And each one names the key of the scenario it came from, which is a
        # different order.
        assert [key.index for key in keys] == [0, 2, 1]
        for key in keys:
            source = written[key.index]
            assert key.value == source.value
            assert key.anchor == source.at.mode.value

    def test_a_muted_track_is_reported_as_not_placed(self):
        scenario = with_intro_and_outro()
        scenario = dataclasses.replace(scenario, tracks=(
            scenario.tracks[0], dataclasses.replace(scenario.tracks[1], muted=True),
        ))

        cta = block(report(scenario, 90.0), "cta")

        assert cta.placed is False
        assert "выключена" in cta.note


class TestGhosts:
    """A rule's output is where it fired on *this* material. Drawn dashed, and
    reported separately from the blocks for exactly that reason (§8.2)."""

    def spoken(self):
        return (
            AssetOption(1, "/m/broll/city.mp4", ("город",), 6.0),
            AssetOption(2, "/m/broll/code.mp4", ("код",), 5.0),
        )

    def with_broll(self) -> model.Scenario:
        scenario = with_intro_and_outro(inserts={"enabled": True})
        rule = model.RuleElement(
            id="broll", rule=model.RULE_KEYWORD_BROLL, label="b-roll по словам", limit=4,
        )
        over = scenario.tracks[1]
        return dataclasses.replace(scenario, tracks=(
            scenario.tracks[0],
            dataclasses.replace(over, elements=over.elements + (rule,)),
        ))

    def test_a_rule_fires_where_the_words_are(self):
        made = report(self.with_broll(), 90.0, assets=self.spoken())

        ghosts = next(rule for rule in made.rules if rule.element_id == "broll").ghosts
        assert ghosts, "the library says the words the mock speaks"
        assert all(ghost.kind == "layer" for ghost in ghosts)
        assert all(0.0 <= ghost.at_sec <= made.duration_sec for ghost in ghosts)

    def test_an_empty_library_fires_nothing_and_says_nothing_else(self):
        """A b-roll rule that can never fire should look exactly like that.
        Inventing fragments to make the picture busier would hide the one
        thing worth seeing."""
        made = report(self.with_broll(), 90.0)

        rule = next(item for item in made.rules if item.element_id == "broll")
        assert rule.ghosts == ()
        assert rule.rule == model.RULE_KEYWORD_BROLL
        assert rule.limit == 4

    def test_a_rule_is_not_a_block(self):
        made = report(self.with_broll(), 90.0, assets=self.spoken())

        assert "broll" not in {item.element_id for item in made.blocks}


class TestTheInventedClip:
    def test_its_joins_become_the_windows_the_spine_is_laid_out_over(self):
        """A mock with no cuts has to say so as one window rather than leave
        it empty: empty means "nothing was measured", and the spine would be
        laid out over the raw length instead (trap 22)."""
        scenario = dataclasses.replace(
            with_intro_and_outro(), mock=model.MockClip(cuts=(10.0, 25.0)),
        )

        facts = mock.facts(scenario, 40.0)

        assert facts.keep == ((0.0, 10.0), (10.0, 25.0), (25.0, 40.0))
        assert facts.cuts == (10.0, 25.0)

    def test_a_cut_past_the_end_is_not_a_cut(self):
        scenario = dataclasses.replace(
            with_intro_and_outro(), mock=model.MockClip(cuts=(10.0, 500.0)),
        )

        assert mock.facts(scenario, 40.0).keep == ((0.0, 10.0), (10.0, 40.0))

    def test_it_says_the_words_the_library_is_tagged_with(self):
        assets = (AssetOption(1, "/m/a.mp4", ("город", "код"), 5.0),)

        facts = mock.facts(with_intro_and_outro(), 30.0, assets=assets)

        spoken = {word["text"] for line in facts.speech for word in line["words"]}
        assert spoken == {"город", "код"}

    def test_and_neutral_filler_when_there_are_none(self):
        """Neutral on purpose: material that says real words invites reading
        meaning into where a rule happened to fire."""
        facts = mock.facts(with_intro_and_outro(), 30.0)

        spoken = {word["text"] for line in facts.speech for word in line["words"]}
        assert spoken == {"раз", "два", "три", "четыре"}

    def test_a_mock_never_names_a_real_file(self):
        """Nothing reads the mock's source, and one that named a path on disk
        would eventually be handed to ffmpeg by somebody who did not know."""
        assert mock.facts(with_intro_and_outro(), 30.0).source_path == mock.SOURCE

    @pytest.mark.parametrize("duration", mock.DURATIONS)
    def test_every_offered_length_compiles(self, duration):
        made = report(with_intro_and_outro(), duration)

        assert made.timeline_sec > 0
        assert made.blocks
