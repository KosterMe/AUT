"""`compile(scenario, facts)` — a montage plus a clip, giving an EDL.

Two kinds of test here. The golden files pin whole compositions as readable
JSON, so a regression shows up as a diff somebody can read rather than as an
assertion about a nested value. The rest check one resolution step at a time.

The one that matters most is `TestTheDefaultScenarioIsTodaysMontage`: a job on
the default scenario has to produce what the pipeline produces today, and while
that holds the conversion has broken nothing.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from montage import composition as comp
from montage import scenario as sc
from montage import subtitles as subtitle_builder
from montage.rules.inserts import AssetOption
from montage.style import StyleSpec

GOLDEN = Path(__file__).parent / "golden" / "scenario"

# Silence taken out of a 90-second window: two pauses, three kept stretches.
KEEP = ((0.0, 30.0), (32.0, 60.0), (62.0, 90.0))
SPEECH = [
    {"start": 12.0, "end": 15.0, "text": "смотрите на машину",
     "words": [{"start": 12.0, "end": 13.0, "word": "смотрите"},
               {"start": 13.0, "end": 13.5, "word": "на"},
               {"start": 13.5, "end": 15.0, "word": "машину"}]},
    {"start": 50.0, "end": 53.0, "text": "и на дорогу",
     "words": [{"start": 50.0, "end": 50.5, "word": "и"},
               {"start": 50.5, "end": 51.0, "word": "на"},
               {"start": 51.0, "end": 53.0, "word": "дорогу"}]},
]
LIBRARY = (
    AssetOption(asset_id=1, path="/media/library/machine.mp4", tags=("машину", "broll"),
                duration_sec=8.0, last_used_rank=0),
    AssetOption(asset_id=2, path="/media/library/road.mp4", tags=("дорогу", "broll"),
                duration_sec=6.0, last_used_rank=1),
    AssetOption(asset_id=3, path="/media/library/bed.mp3", tags=("music",),
                duration_sec=180.0, audio=True, last_used_rank=2),
    AssetOption(asset_id=4, path="/media/library/whoosh.wav", tags=("sfx",),
                duration_sec=1.0, audio=True, last_used_rank=3),
)


def facts(**overrides) -> sc.ClipFacts:
    base = dict(
        source_path="/media/source.mp4", start_sec=10.0, end_sec=100.0,
        width=1920, height=1080, has_audio=True, keep=KEEP, speech=tuple(SPEECH),
        assets=LIBRARY, title="Заголовок клипа", index=1, seed=7,
    )
    return sc.ClipFacts(**{**base, **overrides})


def style(**groups) -> StyleSpec:
    base = StyleSpec.from_settings()
    for name, fields in groups.items():
        base = dataclasses.replace(
            base, **{name: dataclasses.replace(getattr(base, name), **fields)}
        )
    return base


def check_golden(name: str, composition: comp.Composition) -> None:
    """Compare against the recorded EDL, or write it the first time.

    Regenerate deliberately by deleting the file: an assertion that silently
    rewrote its own expectation would pass forever.
    """
    path = GOLDEN / f"{name}.json"
    actual = comp.to_dict(composition)
    if not path.exists():  # pragma: no cover - only on a new golden
        path.write_text(
            json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        pytest.fail(f"wrote a new golden file at {path}; check it in and re-run")
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert actual == expected, (
        f"{name} no longer compiles to what {path.name} records. If the change "
        "is intended, delete the file and re-run to write a new one."
    )


class TestTheDefaultScenarioIsTodaysMontage:
    """§9.1: a job that chose nothing must come out as it does today."""

    def today(self, scenario: sc.Scenario, clip: sc.ClipFacts, *, layout: str):
        """The composition the present pipeline builds from the same inputs.

        Assembled from the same two functions `compiler.plan_vertical_clip`
        calls, so this is the existing behaviour and not a restatement of the
        new code.
        """
        draft = comp.single_source(
            clip.source_path, start_sec=clip.start_sec, end_sec=clip.end_sec,
            keep_segments=clip.keep, canvas=scenario.canvas, style=scenario.style,
            frame=comp.frame_for_layout(layout),
            backdrop=layout == comp.LAYOUT_BLUR,
        )
        cues = subtitle_builder.make_subtitle_cues(
            list(clip.speech), timeline_segments=draft.timeline(),
            fallback_text=clip.title, style=scenario.style.subtitles,
        )
        return dataclasses.replace(
            draft,
            subtitles=comp.SubtitleSpec(cues=tuple(cues), title_text=clip.title),
        )

    def test_a_wide_source_compiles_to_exactly_what_it_compiles_to_now(self):
        scenario = sc.default()

        got, notes = sc.compile(scenario, facts())

        assert got == self.today(scenario, facts(), layout=comp.LAYOUT_BLUR)
        assert notes == ()

    def test_and_so_does_a_source_that_is_already_vertical(self):
        scenario = sc.default()
        clip = facts(width=1080, height=1920)

        got, _ = sc.compile(scenario, clip)

        assert got == self.today(scenario, clip, layout=comp.LAYOUT_FILL)

    def test_the_segments_are_the_kept_stretches_of_the_source(self):
        got, _ = sc.compile(sc.default(), facts())

        assert [(s.source_start_sec, s.source_end_sec) for s in got.spine] == [
            (10.0, 40.0), (42.0, 70.0), (72.0, 100.0),
        ]

    def test_it_is_the_whole_edl_and_not_only_the_segments(self):
        check_golden("default_wide", sc.compile(sc.default(), facts())[0])

    def test_a_vertical_source_has_its_own_golden(self):
        got, _ = sc.compile(sc.default(), facts(width=1080, height=1920))
        check_golden("default_vertical", got)


class TestTheFitDecidesTheFrame:
    """`choose_layout` as a value the scenario carries, not a branch."""

    def spined(self, fit: str, *, backdrop: bool = True, **style_groups) -> sc.Scenario:
        scenario = sc.default(style=style(**style_groups) if style_groups else None)
        spine = dataclasses.replace(
            scenario.tracks[0].elements[0], frame=sc.Frame(fit=fit)
        )
        tracks = (dataclasses.replace(scenario.tracks[0], elements=(spine,)),)
        if backdrop:
            tracks += (scenario.tracks[1],)
        return dataclasses.replace(scenario, tracks=tracks)

    def test_auto_crops_a_source_already_shaped_like_the_canvas(self):
        got, _ = sc.compile(self.spined(sc.FIT_AUTO), facts(width=1080, height=1920))

        assert got.spine[0].frame == comp.FULL_FRAME and not got.spine[0].backdrop

    def test_auto_keeps_a_backdrop_behind_a_wide_one(self):
        got, _ = sc.compile(self.spined(sc.FIT_AUTO), facts())

        assert got.spine[0].frame == comp.CONTAINED and got.spine[0].backdrop

    def test_a_source_of_unknown_shape_keeps_the_backdrop(self):
        """Cropping to a shape nobody measured is the worse guess."""
        got, _ = sc.compile(self.spined(sc.FIT_AUTO), facts(width=0, height=0))

        assert got.spine[0].frame == comp.CONTAINED and got.spine[0].backdrop

    def test_cover_with_nothing_behind_it_fills_the_frame(self):
        got, _ = sc.compile(self.spined(sc.FIT_COVER, backdrop=False), facts())

        assert got.spine[0].frame == comp.FULL_FRAME and not got.spine[0].backdrop

    def test_an_explicit_layout_is_still_honoured(self):
        got, _ = sc.compile(
            self.spined(sc.FIT_AUTO, framing={"layout": comp.LAYOUT_FILL}), facts()
        )

        assert got.spine[0].frame == comp.FULL_FRAME and not got.spine[0].backdrop


def with_rule(*rules: sc.RuleElement) -> sc.Scenario:
    """A plain spine plus these rules, and nothing else in the way."""
    source = sc.Element(
        id="source", duration=sc.Duration(mode=sc.DurationMode.ELASTIC),
    )
    return sc.Scenario(
        name="rules",
        tracks=(
            sc.Track(id="spine", kind=sc.TRACK_SPINE, elements=(source,)),
            sc.Track(id="over", kind=sc.TRACK_OVERLAY, z=1, elements=rules),
        ),
        style=style(),
    )


def talking() -> sc.Scenario:
    """The b-roll-and-music scenario, as §9.2 describes `talking`.

    Not a builtin yet — the four profiles become scenarios at stage 7 — but
    the shape the compiler has to carry, and the one the rules run against.
    """
    source = sc.Element(
        id="source", duration=sc.Duration(mode=sc.DurationMode.ELASTIC),
        frame=sc.Frame(fit=sc.FIT_AUTO),
    )
    backdrop = sc.Element(
        id="backdrop", slot=sc.Slot(kind=sc.SLOT_BLUR_OF, ref="source"),
        duration=sc.Duration(mode=sc.DurationMode.ELASTIC),
    )
    broll = sc.RuleElement(id="broll", rule=sc.RULE_KEYWORD_BROLL, limit=4)
    sounds = sc.RuleElement(id="cuts", rule=sc.RULE_ON_EVERY_CUT)
    bed = sc.Element(
        id="bed", slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag="music"),
        duration=sc.Duration(mode=sc.DurationMode.ELASTIC),
        audio=sc.ElementAudio(enabled=True, loop=True, ducked_by_speech=True),
    )
    return sc.Scenario(
        name="talking",
        tracks=(
            sc.Track(id="spine", kind=sc.TRACK_SPINE, elements=(source,)),
            sc.Track(id="background", kind=sc.TRACK_VIDEO, z=-1, elements=(backdrop,)),
            sc.Track(id="over", kind=sc.TRACK_OVERLAY, z=1, elements=(broll, sounds)),
            sc.Track(id="sound", kind=sc.TRACK_AUDIO, elements=(bed,)),
        ),
        style=style(pacing={"remove_silence": True}),
    )


class TestOverlaysAreAnchoredNotTimed:
    def overlay(self, **kwargs) -> sc.Scenario:
        element = sc.Element(
            id="logo", slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag="broll"),
            **kwargs,
        )
        scenario = sc.default()
        return dataclasses.replace(
            scenario,
            tracks=scenario.tracks + (
                sc.Track(id="over", kind=sc.TRACK_OVERLAY, z=1, elements=(element,)),
            ),
        )

    def test_an_overlay_three_seconds_in_lands_three_seconds_in(self):
        got, _ = sc.compile(
            self.overlay(start=sc.Anchor(mode=sc.AnchorMode.START, value=3.0)), facts()
        )

        assert (got.layers[0].at_sec, got.layers[0].duration_sec) == (3.0, 3.0)

    def test_an_overlay_before_the_end_moves_with_the_clip(self):
        """The reason anchors exist: the same element on two lengths of clip."""
        at_end = self.overlay(start=sc.Anchor(mode=sc.AnchorMode.END, value=5.0))

        long_clip, _ = sc.compile(at_end, facts())
        short_clip, _ = sc.compile(at_end, facts(end_sec=30.0, keep=((0.0, 20.0),)))

        assert long_clip.layers[0].at_sec == 81.0     # a 86s spine after pauses
        assert short_clip.layers[0].at_sec == 15.0    # a 20s one

    def test_an_overlay_on_a_spoken_word_lands_on_the_word(self):
        """On the word and not on its line: karaoke cues are split per word
        group, so "машину" is its own cue at 3.5s of output rather than the
        2.0s where "смотрите на машину" begins. Placing b-roll on the line
        would put the picture a second and a half before the word for it."""
        on_word = self.overlay(
            start=sc.Anchor(mode=sc.AnchorMode.EVENT,
                            event=sc.EventRef(kind="word", word="машину")),
        )

        got, notes = sc.compile(on_word, facts())

        assert got.layers[0].at_sec == 3.5
        assert [n.code for n in notes] == []

    def test_a_full_frame_overlay_and_a_smaller_one_are_different_shapes(self):
        full, _ = sc.compile(self.overlay(frame=sc.Frame()), facts())
        small, _ = sc.compile(
            self.overlay(frame=sc.Frame(width=sc.Animated(40.0), height=sc.Animated(30.0))),
            facts(),
        )

        assert full.layers[0].frame.fills_canvas
        assert not small.layers[0].frame.fills_canvas

    def test_an_overlay_runs_off_the_end_is_cut_short_and_says_so(self):
        late = self.overlay(
            start=sc.Anchor(mode=sc.AnchorMode.END, value=1.0),
            duration=sc.Duration(mode=sc.DurationMode.FIXED, value=10.0),
        )

        got, notes = sc.compile(late, facts())

        assert got.layers[0].duration_sec == 1.0
        assert any(n.code == "duration" for n in notes)

    def test_a_backdrop_is_the_layout_and_not_a_blur_laid_over_the_clip(self):
        """Emitting it twice would put a blurred copy of the clip on the clip."""
        got, _ = sc.compile(sc.default(), facts())

        assert got.layers == ()
        assert got.spine[0].frame == comp.CONTAINED and got.spine[0].backdrop


class TestSlotsBecomePaths:
    def library(self, tag: str, pick: str = sc.PICK_ROTATE) -> sc.Scenario:
        element = sc.Element(
            id="asset", slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag=tag, pick=pick),
            duration=sc.Duration(mode=sc.DurationMode.FIXED, value=2.0),
        )
        scenario = sc.default()
        return dataclasses.replace(
            scenario,
            tracks=scenario.tracks + (
                sc.Track(id="over", kind=sc.TRACK_OVERLAY, z=1, elements=(element,)),
            ),
        )

    def test_a_tag_resolves_to_an_asset_carrying_it(self):
        got, _ = sc.compile(self.library("дорогу"), facts())

        assert got.layers[0].source_path == "/media/library/road.mp4"

    def test_rotation_takes_the_least_recently_used(self):
        """What stops one asset turning up in every clip of a job."""
        got, _ = sc.compile(self.library("broll"), facts())

        assert got.layers[0].source_path == "/media/library/machine.mp4"

    def test_a_tag_nothing_carries_leaves_the_element_out_and_says_so(self):
        got, notes = sc.compile(self.library("самолёт"), facts())

        assert got.layers == ()
        assert [(n.code, n.element_id) for n in notes] == [("no_asset", "asset")]

    def test_the_same_clip_picks_the_same_asset_twice(self):
        """Determinism is a requirement, not a nicety: the fragment cache is
        keyed on the composition, so a clip that compiles differently each
        time has a cache that never hits."""
        scenario = self.library("broll", pick=sc.PICK_RANDOM)

        first, _ = sc.compile(scenario, facts())
        again, _ = sc.compile(scenario, facts())

        assert first.layers[0].source_path == again.layers[0].source_path

    def test_a_text_slot_says_it_needs_a_layer_this_renderer_lacks(self):
        """Loudly: a montage that renders without the text somebody put in it
        is the expensive kind of bug."""
        element = sc.Element(
            id="caption", slot=sc.Slot(kind=sc.SLOT_TEXT, template="{title}"),
            duration=sc.Duration(mode=sc.DurationMode.FIXED, value=2.0),
        )
        scenario = sc.default()
        scenario = dataclasses.replace(
            scenario,
            tracks=scenario.tracks + (
                sc.Track(id="over", kind=sc.TRACK_OVERLAY, z=1, elements=(element,)),
            ),
        )

        got, notes = sc.compile(scenario, facts())

        assert got.layers == ()
        assert any(n.code == "unsupported_slot" for n in notes)


class TestSoundIsOneStructure:
    def test_a_looping_ducked_element_is_the_music_bed(self):
        got, _ = sc.compile(talking(), facts())

        assert got.beds[0].source_path == "/media/library/bed.mp3"
        assert got.beds[0].duck_threshold == 0.03      # ducked by speech

    def test_an_element_that_is_not_ducked_says_so_in_the_only_way_v1_can(self):
        scenario = talking()
        bed = scenario.tracks[3].elements[0]
        unducked = dataclasses.replace(
            bed, audio=dataclasses.replace(bed.audio, ducked_by_speech=False)
        )
        scenario = dataclasses.replace(
            scenario,
            tracks=scenario.tracks[:3] + (
                dataclasses.replace(scenario.tracks[3], elements=(unducked,)),
            ),
        )

        got, _ = sc.compile(scenario, facts())

        # A threshold above any signal is a compressor that never triggers.
        assert got.beds[0].duck_threshold == 1.0

    def test_a_muted_track_contributes_nothing(self):
        scenario = talking()
        scenario = dataclasses.replace(
            scenario,
            tracks=scenario.tracks[:3] + (
                dataclasses.replace(scenario.tracks[3], muted=True),
            ),
        )

        got, _ = sc.compile(scenario, facts())

        assert got.beds == ()


class TestTheSpineIsARectangle:
    """Trap 32, closed: what the spine element says about its rectangle now
    reaches the segments, so an editor can let somebody drag it."""

    def with_spine_frame(self, frame: sc.Frame) -> tuple:
        scenario = sc.Scenario(
            name="framed",
            tracks=(
                sc.Track(id="spine", kind=sc.TRACK_SPINE, elements=(
                    sc.Element(
                        id="source", duration=sc.Duration(mode=sc.DurationMode.ELASTIC),
                        frame=frame,
                    ),
                )),
            ),
            style=style(),
        )
        return sc.compile(scenario, facts())

    def test_the_default_rectangle_still_means_the_layout_decides(self):
        """A scenario nobody has dragged anything in has to compile to the
        graph it always did — `auto` meeting the shape of a wide source."""
        got, _ = self.with_spine_frame(sc.Frame(fit=sc.FIT_AUTO))

        assert got.spine[0].frame == comp.CONTAINED
        assert got.spine[0].backdrop is True

    def test_a_rectangle_of_its_own_is_taken_as_written(self):
        got, _ = self.with_spine_frame(sc.Frame(
            y=sc.Animated(30.0), width=sc.Animated(100.0), height=sc.Animated(60.0),
        ))

        frame = got.spine[0].frame
        assert (frame.y, frame.width, frame.height) == (30.0, 100.0, 60.0)

    def test_a_segments_height_is_a_number_rather_than_the_aspect_ratio(self):
        """A layer may leave its height to the picture, because there is
        something behind it to work it out against. A segment may not."""
        got, _ = self.with_spine_frame(sc.Frame(
            width=sc.Animated(60.0), height=sc.Animated(40.0),
        ))

        assert got.spine[0].frame.height == 40.0

    def test_a_moving_spine_is_refused_with_the_reason(self):
        got, notes = self.with_spine_frame(sc.Frame(x=sc.Animated(50.0, keys=(
            sc.Keyframe(at=sc.Anchor(), value=20.0),
            sc.Keyframe(at=sc.Anchor(mode=sc.AnchorMode.END), value=80.0),
        ))))

        assert got.spine[0].frame.motion is None
        assert any(note.code == "no_spine_animation" for note in notes)


class TestKeyframesReachTheRenderer:
    """Stage 6's floor: a keyframe written in a scenario has to arrive at the
    EDL as something the renderer can actually move, and everything else has
    to keep compiling to exactly what it compiled to before."""

    def moving(self, frame: sc.Frame) -> comp.Composition:
        element = sc.Element(
            id="mover", slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag="broll"),
            start=sc.Anchor(mode=sc.AnchorMode.START, value=0.0),
            duration=sc.Duration(mode=sc.DurationMode.FIXED, value=10.0),
            frame=frame,
        )
        scenario = sc.Scenario(
            name="moving",
            tracks=(
                sc.Track(id="spine", kind=sc.TRACK_SPINE, elements=(
                    sc.Element(id="source", duration=sc.Duration(mode=sc.DurationMode.ELASTIC)),
                )),
                sc.Track(id="over", kind=sc.TRACK_OVERLAY, z=1, elements=(element,)),
            ),
            style=style(),
        )
        got, _ = sc.compile(scenario, facts())
        return got

    def test_a_still_overlay_carries_no_motion_at_all(self):
        """`is_static` is not decoration: a scenario without animation must
        not pay for animation it does not have (§4.3)."""
        got = self.moving(sc.Frame(x=sc.Animated(25.0)))

        assert got.layers[0].frame.motion is None
        assert got.layers[0].frame.moves is False

    def test_keyframes_become_a_polyline_in_output_seconds(self):
        got = self.moving(sc.Frame(x=sc.Animated(0.0, keys=(
            sc.Keyframe(at=sc.Anchor(mode=sc.AnchorMode.START, value=0.0), value=-20.0),
            sc.Keyframe(at=sc.Anchor(mode=sc.AnchorMode.START, value=4.0), value=120.0),
        ))))

        assert got.layers[0].frame.motion.x == ((0.0, -20.0), (4.0, 120.0))
        assert got.layers[0].frame.motion.y == ()

    def test_an_anchored_keyframe_moves_with_the_clip(self):
        """The reason a keyframe carries an anchor: "leave half a second
        before the end" has to mean that on a clip of any length."""
        got = self.moving(sc.Frame(x=sc.Animated(50.0, keys=(
            sc.Keyframe(at=sc.Anchor(mode=sc.AnchorMode.END, offset_sec=-2.0), value=50.0),
            sc.Keyframe(at=sc.Anchor(mode=sc.AnchorMode.END, offset_sec=-0.5), value=150.0),
        ))))

        clip = got.duration_sec
        assert got.layers[0].frame.motion.x == (
            (round(clip - 2.0, 3), 50.0), (round(clip - 0.5, 3), 150.0),
        )

    def test_a_size_that_moves_reaches_the_edl_too(self):
        """Measured on this build (§7.2): `scale` takes an expression with
        `eval=frame`, and `overlay` then reads the layer's current `w`/`h`."""
        got = self.moving(sc.Frame(width=sc.Animated(40.0, keys=(
            sc.Keyframe(at=sc.Anchor(mode=sc.AnchorMode.START, value=0.0), value=20.0),
            sc.Keyframe(at=sc.Anchor(mode=sc.AnchorMode.START, value=4.0), value=80.0),
        ))))

        assert got.layers[0].frame.motion.width == ((0.0, 20.0), (4.0, 80.0))
        assert got.layers[0].frame.motion.resizes is True

    def test_an_angle_that_moves_reaches_the_edl_in_degrees(self):
        """Degrees here because that is what somebody types; the renderer
        converts, because that is what ffmpeg reads."""
        got = self.moving(sc.Frame(rotate=sc.Animated(0.0, keys=(
            sc.Keyframe(at=sc.Anchor(mode=sc.AnchorMode.START, value=0.0), value=-15.0),
            sc.Keyframe(at=sc.Anchor(mode=sc.AnchorMode.START, value=4.0), value=15.0),
        ))))

        assert got.layers[0].frame.motion.rotate == ((0.0, -15.0), (4.0, 15.0))

    def test_opacity_is_the_one_that_still_cannot_move(self):
        """And it says so. `geq` was measured at ×23 — a different
        conversation from "does it work"."""
        got, notes = sc.compile(
            with_rule(),  # a plain spine; the overlay is added below
            facts(),
        )
        element = sc.Element(
            id="fading", slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag="broll"),
            duration=sc.Duration(mode=sc.DurationMode.FIXED, value=5.0),
            frame=sc.Frame(opacity=sc.Animated(1.0, keys=(
                sc.Keyframe(at=sc.Anchor(), value=0.2),
                sc.Keyframe(at=sc.Anchor(mode=sc.AnchorMode.END), value=1.0),
            ))),
        )
        scenario = sc.Scenario(
            name="fading",
            tracks=(
                sc.Track(id="spine", kind=sc.TRACK_SPINE, elements=(
                    sc.Element(id="source", duration=sc.Duration(mode=sc.DurationMode.ELASTIC)),
                )),
                sc.Track(id="over", kind=sc.TRACK_OVERLAY, z=1, elements=(element,)),
            ),
            style=style(),
        )

        got, notes = sc.compile(scenario, facts())

        assert [n.code for n in notes if n.code == "no_animation"] == ["no_animation"]
        assert "opacity" in next(n for n in notes if n.code == "no_animation").message


class TestRulesExpandAgainstTheClip:
    def test_keyword_broll_lands_on_the_words_that_name_it(self):
        got, _ = sc.compile(talking(), facts())

        assert [layer.source_path for layer in got.layers] == [
            "/media/library/machine.mp4", "/media/library/road.mp4",
        ]

    def test_the_limits_the_readme_earned_are_still_enforced(self):
        """Never over the hook, and never more than a third of the clip."""
        got, _ = sc.compile(talking(), facts())
        spine = got.duration_sec

        assert all(layer.at_sec >= 2.5 for layer in got.layers)
        assert sum(layer.duration_sec for layer in got.layers) <= spine * 0.35

    def test_a_rule_with_an_empty_library_does_nothing_rather_than_failing(self):
        got, _ = sc.compile(talking(), facts(assets=()))

        assert got.layers == () and got.beds == ()

    def test_sounds_land_on_the_cuts_once_the_broll_exists(self):
        """B-roll first, deliberately: an insert appearing is one of the
        moments a transition sound belongs on."""
        got, _ = sc.compile(talking(), facts())

        assert got.stingers
        assert all(effect.source_path == "/media/library/whoosh.wav" for effect in got.stingers)

    def test_cadence_places_its_template_on_a_beat(self):
        """A logo every thirty seconds is a thing people ask for, and writing
        it as four elements with four anchors is how a scenario stops
        surviving a clip of a different length."""
        logo = sc.Element(
            id="logo", slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag="broll"),
            duration=sc.Duration(mode=sc.DurationMode.FIXED, value=2.0),
        )
        rule = sc.RuleElement(
            id="beat", rule=sc.RULE_CADENCE, template=logo, params={"every_sec": 20.0},
            guard_head_sec=5.0, guard_tail_sec=5.0, min_gap_sec=1.0, limit=10,
        )

        got, _ = sc.compile(with_rule(rule), facts())

        assert [layer.at_sec for layer in got.layers] == [5.0, 25.0, 45.0, 65.0]
        assert all(layer.duration_sec == 2.0 for layer in got.layers)

    def test_cadence_stops_at_the_limit_it_was_given(self):
        logo = sc.Element(
            id="logo", slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag="broll"),
            duration=sc.Duration(mode=sc.DurationMode.FIXED, value=2.0),
        )
        rule = sc.RuleElement(
            id="beat", rule=sc.RULE_CADENCE, template=logo, params={"every_sec": 10.0},
            limit=2, min_gap_sec=1.0,
        )

        got, _ = sc.compile(with_rule(rule), facts())

        assert len(got.layers) == 2

    def test_a_rule_never_takes_more_of_the_clip_than_its_share(self):
        """The limit the README earned, applied to a rule that carries it
        itself rather than to a policy object beside it."""
        long_one = sc.Element(
            id="long", slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag="broll"),
            duration=sc.Duration(mode=sc.DurationMode.FIXED, value=20.0),
        )
        rule = sc.RuleElement(
            id="beat", rule=sc.RULE_CADENCE, template=long_one,
            params={"every_sec": 5.0}, limit=20, min_gap_sec=1.0, max_share=0.25,
        )

        got, _ = sc.compile(with_rule(rule), facts())
        covered = sum(layer.duration_sec for layer in got.layers)

        assert covered <= got.duration_sec * 0.25 + 0.001
        assert got.layers, "a quarter of the clip is still some of it"

    def test_a_template_that_is_a_sound_makes_sounds_not_pictures(self):
        """The same distinction an element makes anywhere else: a thing with
        `audio.enabled` is a sound. A rule does not need a second vocabulary
        for it."""
        sting = sc.Element(
            id="sting", slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag="sfx"),
            duration=sc.Duration(mode=sc.DurationMode.FIXED, value=1.0),
            audio=sc.ElementAudio(enabled=True),
        )
        rule = sc.RuleElement(
            id="beat", rule=sc.RULE_CADENCE, template=sting,
            params={"every_sec": 30.0}, min_gap_sec=1.0,
        )

        got, _ = sc.compile(with_rule(rule), facts())

        assert not got.layers
        # Three beats on this clip, and the same sound each time: one sting
        # repeating is what a sting is, so the library is not asked to have
        # three of them.
        assert [track.at_sec for track in got.audio] == [2.5, 32.5, 62.5]
        assert {track.source_path for track in got.audio} == {"/media/library/whoosh.wav"}

    def test_on_loudest_goes_where_the_clip_is_loudest(self):
        peak = sc.Element(
            id="peak", slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag="broll"),
            duration=sc.Duration(mode=sc.DurationMode.FIXED, value=2.0),
        )
        rule = sc.RuleElement(
            id="loud", rule=sc.RULE_ON_LOUDEST, template=peak, limit=2,
            min_gap_sec=1.0, guard_head_sec=0.0, guard_tail_sec=0.0,
        )
        # Quiet everywhere except two moments, and the louder of them first.
        curve = tuple((float(second), -40.0) for second in range(0, 90))
        curve = curve[:20] + ((20.0, -5.0),) + curve[21:70] + ((70.0, -12.0),) + curve[71:]

        got, _ = sc.compile(with_rule(rule), facts(loudness=curve))

        # 20 and 70 in clip time. Two pauses were cut before the second one
        # (30–32 and 60–62), so it lands four seconds earlier in the output —
        # which is the whole reason the mapping exists.
        assert [layer.at_sec for layer in got.layers] == [20.0, 66.0]

    def test_on_loudest_ignores_a_moment_that_was_cut_out(self):
        """A peak inside a removed pause is not in the clip at all, and
        placing something at it would land after the moment it was chosen
        for."""
        peak = sc.Element(
            id="peak", slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag="broll"),
            duration=sc.Duration(mode=sc.DurationMode.FIXED, value=2.0),
        )
        rule = sc.RuleElement(
            id="loud", rule=sc.RULE_ON_LOUDEST, template=peak, limit=1,
            guard_head_sec=0.0, guard_tail_sec=0.0,
        )
        # The loudest reading sits in the 30–32 pause that silence removal cut.
        curve = ((31.0, -1.0), (50.0, -20.0), (10.0, -60.0))

        got, _ = sc.compile(with_rule(rule), facts(loudness=curve))

        assert [layer.at_sec for layer in got.layers] == [48.0]

    def test_on_loudest_without_a_curve_does_nothing(self):
        """And says nothing either: the scenario asked for the fact, and
        whether it was measured is the caller's business."""
        peak = sc.Element(
            id="peak", slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag="broll"),
            duration=sc.Duration(mode=sc.DurationMode.FIXED, value=2.0),
        )
        rule = sc.RuleElement(id="loud", rule=sc.RULE_ON_LOUDEST, template=peak)

        got, notes = sc.compile(with_rule(rule), facts(loudness=()))

        assert not got.layers
        assert not [note for note in notes if note.code == "rule_without_template"]

    def test_a_scenario_with_on_loudest_asks_for_the_curve(self):
        """Which is what makes the fact get measured at all — it is derived
        from the scenario, never configured beside it (§6.1)."""
        peak = sc.Element(id="peak", slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag="broll"))
        rule = sc.RuleElement(id="loud", rule=sc.RULE_ON_LOUDEST, template=peak)

        assert sc.FactKind.LOUDNESS in sc.required_facts(with_rule(rule))

    def test_a_rule_with_nothing_to_place_says_so(self):
        """A rule is an element that says how to make several, so what it
        makes is an element — its template. Without one there is nothing to
        place, and doing nothing quietly is how an operator spends an evening
        wondering why the automation never fires."""
        scenario = with_rule(sc.RuleElement(id="cadence", rule=sc.RULE_CADENCE))

        _, notes = sc.compile(scenario, facts())

        assert any(n.code == "rule_without_template" for n in notes)

    def test_a_rule_nobody_has_implemented_says_so(self, monkeypatch):
        """The branch that catches the next rule: added to the model, and the
        compiler half forgotten. It has to be visible rather than silent, so
        the test adds one the same way that mistake would."""
        monkeypatch.setattr(sc.model, "RULES", sc.model.RULES + ("телепатия",))
        scenario = with_rule(sc.RuleElement(id="psychic", rule="телепатия"))

        _, notes = sc.compile(scenario, facts())

        assert any(n.code == "unimplemented_rule" for n in notes)

    def test_a_rule_on_the_spine_is_refused_because_it_would_move_the_clip(self):
        scenario = sc.default()
        rule = sc.RuleElement(id="bad", rule=sc.RULE_KEYWORD_BROLL)
        scenario = dataclasses.replace(
            scenario,
            tracks=(dataclasses.replace(
                scenario.tracks[0],
                elements=scenario.tracks[0].elements + (rule,),
            ),) + scenario.tracks[1:],
        )

        _, notes = sc.compile(scenario, facts())

        assert any(n.code == "rule_on_spine" for n in notes)

    def test_the_dressed_clip_has_its_own_golden(self):
        check_golden("talking", sc.compile(talking(), facts())[0])


class TestAClipTooShortForTheScenario:
    def with_outro(self, *, optional: bool) -> sc.Scenario:
        source = sc.Element(id="source", duration=sc.Duration(mode=sc.DurationMode.ELASTIC))
        outro = sc.Element(
            id="outro", slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag="broll"),
            duration=sc.Duration(mode=sc.DurationMode.FIXED, value=8.0),
            optional=optional,
        )
        return sc.Scenario(name="t", tracks=(
            sc.Track(id="spine", kind=sc.TRACK_SPINE, elements=(source, outro)),
        ))

    def test_an_expendable_outro_is_dropped_and_the_clip_survives(self):
        short = facts(end_sec=16.0, keep=((0.0, 6.0),))

        got, notes = sc.compile(self.with_outro(optional=True), short)

        assert got.spine
        assert any(n.code == "dropped" and n.element_id == "outro" for n in notes)

    def test_an_outro_that_cannot_be_dropped_truncates_and_warns(self):
        short = facts(end_sec=16.0, keep=((0.0, 6.0),))

        got, notes = sc.compile(self.with_outro(optional=False), short)

        assert got.spine
        assert any(n.code == "truncated" for n in notes)

    def test_a_scenario_with_no_spine_says_so_rather_than_emitting_nothing(self):
        empty = sc.Scenario(name="t", tracks=())

        got, notes = sc.compile(empty, facts())

        assert got.spine            # the whole clip, as a fallback
        assert {n.code for n in notes} >= {"no_spine", "empty_spine"}


class TestAnchorCyclesAreReported:
    def test_two_overlays_placed_after_each_other_are_reported_not_hung_on(self):
        x = sc.Element(id="x", start=sc.Anchor(mode=sc.AnchorMode.AFTER, ref="y"),
                       slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag="broll"))
        y = sc.Element(id="y", start=sc.Anchor(mode=sc.AnchorMode.AFTER, ref="x"),
                       slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag="broll"))
        scenario = sc.default()
        scenario = dataclasses.replace(
            scenario,
            tracks=scenario.tracks + (
                sc.Track(id="over", kind=sc.TRACK_OVERLAY, z=1, elements=(x, y)),
            ),
        )

        got, notes = sc.compile(scenario, facts())

        assert any(n.code == "anchor_cycle" for n in notes)
        assert got.spine      # and the clip is still made


class TestASpineOfSeveralElements:
    """The generalisation the default scenario does not exercise."""

    def hooked(self) -> sc.Scenario:
        hook = sc.Element(
            id="hook", slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag="broll"),
            duration=sc.Duration(mode=sc.DurationMode.FIXED, value=5.0),
        )
        body = sc.Element(
            id="body", duration=sc.Duration(mode=sc.DurationMode.ELASTIC),
            frame=sc.Frame(fit=sc.FIT_AUTO),
        )
        return sc.Scenario(name="hooked", tracks=(
            sc.Track(id="spine", kind=sc.TRACK_SPINE, elements=(hook, body)),
        ))

    def test_each_element_plays_its_own_material_in_turn(self):
        got, notes = sc.compile(self.hooked(), facts())

        assert [s.source_path for s in got.spine] == [
            "/media/library/machine.mp4", "/media/source.mp4",
            "/media/source.mp4", "/media/source.mp4",
        ]
        assert notes == ()

    def test_the_source_element_takes_what_is_left_of_the_kept_material(self):
        """86s of kept material, 5 of them spent on the hook, 81 left — and the
        kept windows are consumed in order rather than restarted."""
        got, _ = sc.compile(self.hooked(), facts())
        source = [s for s in got.spine if s.source_path == "/media/source.mp4"]

        assert sum(s.duration_sec for s in source) == 81.0
        assert (source[0].source_start_sec, source[0].source_end_sec) == (10.0, 40.0)
        assert source[-1].source_end_sec == 95.0     # 5s short of the window

    def test_a_hook_on_the_spine_pushes_the_clip_it_precedes(self):
        got, _ = sc.compile(self.hooked(), facts())

        assert got.spine[0].duration_sec == 5.0
        assert got.duration_sec == 86.0


class TestTracksStackByTheirZ:
    """v1 applies inserts in list order, so the list is where z has to land."""

    def stacked(self, *zs: int) -> sc.Scenario:
        scenario = sc.default()
        tracks = scenario.tracks
        for z in zs:
            element = sc.Element(
                id=f"layer{z}", slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag="broll"),
                duration=sc.Duration(mode=sc.DurationMode.FIXED, value=2.0),
                frame=sc.Frame(width=sc.Animated(50.0), height=sc.Animated(50.0)),
            )
            tracks += (sc.Track(id=f"t{z}", kind=sc.TRACK_OVERLAY, z=z,
                                elements=(element,)),)
        return dataclasses.replace(scenario, tracks=tracks)

    def test_the_lowest_layer_is_applied_first(self):
        got, _ = sc.compile(self.stacked(5, 1), facts())

        # Written 5 then 1; emitted 1 then 5, so 5 ends up on top.
        assert [layer.source_path for layer in got.layers] == [
            "/media/library/machine.mp4", "/media/library/road.mp4",
        ]
        assert len(got.layers) == 2


class TestOneFragmentOncePerClip:
    """The constraint the rules always enforced, applied to explicit slots too."""

    def layers(self, count: int, tag: str = "broll") -> sc.Scenario:
        scenario = sc.default()
        tracks = scenario.tracks
        for n in range(count):
            element = sc.Element(
                id=f"layer{n}", slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag=tag),
                start=sc.Anchor(mode=sc.AnchorMode.START, value=float(n * 10)),
                duration=sc.Duration(mode=sc.DurationMode.FIXED, value=2.0),
            )
            tracks += (sc.Track(id=f"t{n}", kind=sc.TRACK_OVERLAY, z=n,
                                elements=(element,)),)
        return dataclasses.replace(scenario, tracks=tracks)

    def test_two_slots_on_one_tag_take_different_assets(self):
        got, _ = sc.compile(self.layers(2), facts())

        assert len({layer.source_path for layer in got.layers}) == 2

    def test_a_library_too_small_repeats_rather_than_leaving_a_hole(self):
        """The same shot twice beats an element that renders as nothing."""
        one_asset = (LIBRARY[0],)

        got, notes = sc.compile(self.layers(3), facts(assets=one_asset))

        assert len(got.layers) == 3
        assert len({layer.source_path for layer in got.layers}) == 1
        assert not any(n.code == "no_asset" for n in notes)

    def test_the_spine_and_an_overlay_do_not_show_the_same_fragment(self):
        hook = sc.Element(
            id="hook", slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag="broll"),
            duration=sc.Duration(mode=sc.DurationMode.FIXED, value=4.0),
        )
        body = sc.Element(id="body", duration=sc.Duration(mode=sc.DurationMode.ELASTIC))
        over = sc.Element(
            id="over", slot=sc.Slot(kind=sc.SLOT_LIBRARY, tag="broll"),
            start=sc.Anchor(mode=sc.AnchorMode.START, value=20.0),
            duration=sc.Duration(mode=sc.DurationMode.FIXED, value=2.0),
        )
        scenario = sc.Scenario(name="t", tracks=(
            sc.Track(id="spine", kind=sc.TRACK_SPINE, elements=(hook, body)),
            sc.Track(id="over", kind=sc.TRACK_OVERLAY, z=1, elements=(over,)),
        ))

        got, _ = sc.compile(scenario, facts())
        from_library = {
            s.source_path for s in got.spine if "library" in s.source_path
        } | {layer.source_path for layer in got.layers}

        assert len(from_library) == 2
