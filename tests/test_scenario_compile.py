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

    def test_a_rule_nobody_has_implemented_says_so(self):
        scenario = talking()
        cadence = sc.RuleElement(id="cadence", rule=sc.RULE_CADENCE)
        scenario = dataclasses.replace(
            scenario,
            tracks=scenario.tracks[:2] + (
                dataclasses.replace(scenario.tracks[2], elements=(cadence,)),
            ) + scenario.tracks[3:],
        )

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
