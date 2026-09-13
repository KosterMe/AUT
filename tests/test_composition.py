"""The montage model: what a clip is made of, before ffmpeg sees it.

Pure values in, pure values out — which is the point of having the model at
all. Every rule here used to be implicit in the shape of a filter graph.
"""
from __future__ import annotations

import pytest

from montage import composition as comp

# What `broll_pip` named: a rectangle in the corner rather than a kind.
PIP = comp.frame_for_kind(comp.INSERT_PIP, comp.StyleSpec.from_settings().inserts, comp.Canvas())
from montage.subtitles import SubtitleCue


def test_single_source_without_keep_segments_is_one_continuous_cut():
    result = comp.single_source("a.mp4", start_sec=90.0, end_sec=120.0)

    assert len(result.spine) == 1
    assert result.spine[0].source_start_sec == 90.0
    assert result.spine[0].source_end_sec == 120.0
    assert result.duration_sec == 30.0


def test_keep_segments_are_relative_to_the_clip_start():
    """Silence detection reports windows inside the clip, not inside the file.

    Reading them as absolute source times would cut from the wrong place —
    a clip starting at minute nine would render the first nine minutes.
    """
    result = comp.single_source(
        "a.mp4", start_sec=540.0, end_sec=580.0, keep_segments=[(0.0, 12.0), (18.0, 40.0)]
    )

    assert [(s.source_start_sec, s.source_end_sec) for s in result.spine] == [
        (540.0, 552.0),
        (558.0, 580.0),
    ]
    # The removed six seconds are gone from the output, not merely skipped over.
    assert result.duration_sec == 34.0


def test_timeline_maps_source_time_onto_output_time():
    """What subtitle cues are projected through: a word spoken at source
    second 558 has to be drawn at output second 12."""
    result = comp.single_source(
        "a.mp4", start_sec=540.0, end_sec=580.0, keep_segments=[(0.0, 12.0), (18.0, 40.0)]
    )

    timeline = result.timeline()

    assert [(t.output_start_sec, t.output_end_sec) for t in timeline] == [(0.0, 12.0), (12.0, 34.0)]
    assert timeline[1].source_start_sec == 558.0


def test_slivers_are_dropped_but_never_leave_a_clip_with_nothing():
    """Silence detection produces sub-frame windows. Losing one is fine;
    losing the clip is not."""
    result = comp.single_source(
        "a.mp4", start_sec=0.0, end_sec=10.0, keep_segments=[(0.0, 0.01), (1.0, 8.0)]
    )
    assert len(result.spine) == 1
    assert result.spine[0].source_start_sec == 1.0

    empty = comp.single_source("a.mp4", start_sec=0.0, end_sec=10.0, keep_segments=[(0.0, 0.001)])
    assert len(empty.spine) == 1


def test_source_paths_are_deduplicated_in_first_use_order():
    result = comp.Composition(
        spine=(
            comp.Segment("a.mp4", 0.0, 5.0),
            comp.Segment("a.mp4", 10.0, 15.0),
            comp.Segment("b.mp4", 0.0, 5.0),
        ),
        layers=(comp.Layer("c.mp4", frame=PIP, at_sec=1.0, duration_sec=2.0),),
    )

    assert result.source_paths == ("a.mp4", "b.mp4", "c.mp4")


def test_an_insert_cannot_run_past_the_end_of_the_clip():
    with pytest.raises(ValueError, match="past the end"):
        comp.Composition(
            spine=(comp.Segment("a.mp4", 0.0, 10.0),),
            layers=(comp.Layer("b.mp4", at_sec=9.0, duration_sec=3.0),),
        )


def test_a_composition_needs_at_least_one_segment():
    with pytest.raises(ValueError, match="at least one segment"):
        comp.Composition(spine=())


def test_a_segment_shorter_than_the_floor_is_rejected():
    with pytest.raises(ValueError, match="floor"):
        comp.Segment("a.mp4", 5.0, 5.01)


def test_a_segment_has_no_layout_left_to_get_wrong():
    """Three words that named three pictures became a rectangle and a flag,
    and a rectangle cannot be misspelled. What a segment still refuses is a
    source it cannot read and a piece too short to cut."""
    assert comp.Segment("a.mp4", 0.0, 5.0).frame == comp.CONTAINED
    with pytest.raises(ValueError, match="source path"):
        comp.Segment("", 0.0, 5.0)


def test_a_layer_has_no_kind_left_to_get_wrong():
    """`broll_full` and `broll_pip` were a rectangle covering the canvas and a
    rectangle in the corner. A rectangle cannot be misspelled, so the whole
    class of "unknown insert kind" is gone rather than moved."""
    with pytest.raises(ValueError, match="positive duration"):
        comp.Layer("b.mp4", at_sec=0.0, duration_sec=0.0)


# --- storing a composition --------------------------------------------------


def test_a_composition_round_trips_through_json():
    """What makes a finished clip inspectable, and re-renderable without
    re-deciding anything: everything that shaped it survives the trip."""
    original = comp.Composition(
        spine=(
            comp.Segment("/media/a.mp4", 10.0, 20.0),
            comp.Segment("/media/a.mp4", 40.0, 45.0),
        ),
        layers=(comp.Layer("/media/broll.mp4", at_sec=4.0, duration_sec=2.0),),
        subtitles=comp.SubtitleSpec(
            cues=(SubtitleCue(start_sec=1.0, end_sec=1.4, text="привет"),),
            title_text="часть 1",
        ),
        audio=(
            comp.AudioTrack("/media/bed.mp3", gain_db=-18.0, loop=True),
            comp.AudioTrack("/media/whoosh.wav", at_sec=9.9, duration_sec=1.0),
        ),
        style=comp.StyleSpec.from_settings().merged({"framing": {"zoom": 1.35}}),
    )

    restored = comp.from_dict(comp.to_dict(original))

    assert restored == original
    assert restored.style.framing.zoom == 1.35


def test_a_stored_composition_from_an_older_version_still_loads():
    """Fields that did not exist when it was written take their defaults —
    the alternative is a clip nobody can re-render after an upgrade."""
    restored = comp.from_dict(
        {"segments": [{"source_path": "/media/a.mp4",
                       "source_start_sec": 0.0, "source_end_sec": 12.0}]}
    )

    assert restored.duration_sec == 12.0
    assert restored.style.delivery.width == 1080


# --- excerpts, which is what a preview is -----------------------------------


def build(**overrides) -> comp.Composition:
    kwargs = dict(
        spine=(
            comp.Segment("/media/a.mp4", 100.0, 110.0),
            comp.Segment("/media/a.mp4", 200.0, 210.0),
        ),
    )
    kwargs.update(overrides)
    return comp.Composition(**kwargs)


def test_an_excerpt_takes_the_source_time_the_window_lands_on():
    """Output second 12 is source second 202 when the first segment ended at
    10 — getting this wrong previews a different part of the video."""
    window = comp.excerpt(build(), at_sec=12.0, duration_sec=4.0)

    assert len(window.spine) == 1
    assert window.spine[0].source_start_sec == 202.0
    assert window.spine[0].source_end_sec == 206.0
    assert window.duration_sec == 4.0


def test_an_excerpt_spanning_a_cut_keeps_both_sides_of_it():
    window = comp.excerpt(build(), at_sec=8.0, duration_sec=4.0)

    assert [(s.source_start_sec, s.source_end_sec) for s in window.spine] == [
        (108.0, 110.0), (200.0, 202.0)
    ]


def test_an_excerpt_moves_everything_laid_over_the_clip_with_it():
    composition = build(
        layers=(comp.Layer("/media/b.mp4", at_sec=11.0, duration_sec=2.0),),
        subtitles=comp.SubtitleSpec(
            cues=(
                SubtitleCue(start_sec=2.0, end_sec=2.4, text="early"),
                SubtitleCue(start_sec=11.5, end_sec=11.9, text="inside"),
            ),
        ),
        audio=(comp.AudioTrack("/media/w.wav", at_sec=11.2, duration_sec=1.0),),
    )

    window = comp.excerpt(composition, at_sec=10.0, duration_sec=4.0)

    assert window.layers[0].at_sec == 1.0
    assert [cue.text for cue in window.subtitles.cues] == ["inside"]
    assert window.subtitles.cues[0].start_sec == 1.5
    assert window.stingers[0].at_sec == pytest.approx(1.2)


def test_an_excerpt_moves_a_curve_onto_its_own_clock():
    """A preview of a moving layer that showed it parked where it starts
    would be a preview of the one thing it is there to check."""
    composition = build(
        layers=(
            comp.Layer(
                "/media/b.mp4", at_sec=11.0, duration_sec=2.0,
                frame=comp.Frame(
                    width=40.0,
                    motion=comp.Motion(x=((11.0, -20.0), (13.0, 120.0))),
                ),
            ),
        ),
    )

    window = comp.excerpt(composition, at_sec=10.0, duration_sec=4.0)

    assert window.layers[0].frame.motion.x == ((1.0, -20.0), (3.0, 120.0))


def test_an_excerpt_leaves_a_still_layer_exactly_as_it_was():
    composition = build(layers=(comp.Layer("/media/b.mp4", at_sec=11.0, duration_sec=2.0),))

    window = comp.excerpt(composition, at_sec=10.0, duration_sec=4.0)

    assert window.layers[0].frame.motion is None


def test_an_excerpt_advances_the_music_rather_than_restarting_it():
    """A preview of the last ten seconds should hear the part of the track
    that plays there."""
    window = comp.excerpt(
        build(audio=(comp.AudioTrack("/media/bed.mp3", loop=True),)), at_sec=12.0,
                          duration_sec=4.0)

    assert window.beds[0].source_start_sec == 12.0


def test_a_window_past_the_end_is_refused():
    with pytest.raises(ValueError):
        comp.excerpt(build(), at_sec=90.0, duration_sec=4.0)


def test_scaling_shrinks_the_canvas_and_the_type_with_it():
    """Everything else in this model is a share of the frame, so the font
    size is the one thing that would not survive being scaled."""
    small = comp.scaled(build(), 0.5)

    assert (small.canvas.width, small.canvas.height) == (540, 960)
    assert small.style.subtitles.font_size == 41
    assert small.style.delivery.width == 540
    # The framing is untouched: it is expressed relative to the canvas.
    assert small.style.framing.zoom == build().style.framing.zoom


# --- reading what older versions wrote --------------------------------------


V1_DOCUMENT = {
    "version": 2,
    "canvas": {"width": 1080, "height": 1920, "fps": 30},
    "segments": [
        {"source_path": "/media/source.mp4", "source_start_sec": 10.0,
         "source_end_sec": 40.0, "layout": "blur"},
    ],
    "inserts": [
        {"kind": "broll_full", "source_path": "/library/a.mp4",
         "at_sec": 4.0, "duration_sec": 2.0},
        {"kind": "broll_pip", "source_path": "/library/b.mp4",
         "at_sec": 12.0, "duration_sec": 3.0, "still": True},
    ],
    "music": {"source_path": "/library/bed.mp3", "gain_db": -20.0, "start_sec": 30.0,
              "fade_in_sec": 0.6, "fade_out_sec": 1.2, "duck_threshold": 0.03,
              "duck_ratio": 8.0, "duck_attack_ms": 20.0, "duck_release_ms": 300.0},
    "effects": [
        {"source_path": "/library/whoosh.wav", "at_sec": 9.9,
         "duration_sec": 1.0, "gain_db": -8.0},
    ],
    "subtitles": {"cues": [{"start_sec": 1.0, "end_sec": 1.4, "text": "привет"}],
                  "title_text": "часть 1"},
}


class TestAnOlderCompositionStillOpens:
    """Every clip rendered before the layered model carries one of these.

    That stored composition is the only account of what the clip was made of,
    so a reader that could not open it would turn every finished clip into a
    video nobody can explain. The kinds and the two audio entities are read
    back as what they always meant.
    """

    def lifted(self) -> comp.Composition:
        return comp.from_dict(V1_DOCUMENT)

    def test_the_segments_become_the_spine(self):
        assert [s.source_start_sec for s in self.lifted().spine] == [10.0]

    def test_a_full_frame_insert_becomes_a_layer_covering_the_canvas(self):
        full = self.lifted().layers[0]

        assert full.source_path == "/library/a.mp4"
        assert full.frame.fills_canvas

    def test_a_pip_insert_becomes_the_rectangle_it_always_was(self):
        """Pixel for pixel: the corner the renderer used to compute inline."""
        pip = next(l for l in self.lifted().layers if l.source_path == "/library/b.mp4")
        policy = comp.StyleSpec.from_settings().inserts
        canvas = comp.Canvas()

        box_width = max(2, int(canvas.width * policy.pip_width_share) // 2 * 2)
        assert pip.frame.box(canvas) == (
            canvas.width - box_width - policy.pip_margin_px,
            int(canvas.height * policy.pip_top_share),
            box_width,
            0,      # the aspect ratio decides, as `scale=W:-2` did
        )

    def test_a_still_stays_a_still(self):
        pip = next(l for l in self.lifted().layers if l.source_path == "/library/b.mp4")

        assert pip.still is True

    def test_the_music_bed_becomes_a_looping_ducked_track(self):
        beds = self.lifted().beds

        assert [bed.source_path for bed in beds] == ["/library/bed.mp3"]
        assert beds[0].loop is True
        assert beds[0].ducks is True

    def test_the_beds_offset_is_read_as_an_offset_into_the_track(self):
        """`start_sec` could be read either way, which is why it is renamed:
        it is where in the *music* to begin, so a job's clips do not all open
        on the same four bars."""
        assert self.lifted().beds[0].source_start_sec == 30.0
        assert self.lifted().beds[0].at_sec == 0.0

    def test_the_sound_effects_become_tracks_that_do_not_loop(self):
        stingers = self.lifted().stingers

        assert [s.source_path for s in stingers] == ["/library/whoosh.wav"]
        assert stingers[0].loop is False
        assert stingers[0].ducks is False

    def test_the_subtitles_come_through_unchanged(self):
        subtitles = self.lifted().subtitles

        assert subtitles.title_text == "часть 1"
        assert subtitles.cues[0].text == "привет"

    def test_what_it_reads_it_can_write_back(self):
        once = comp.from_dict(V1_DOCUMENT)
        twice = comp.from_dict(comp.to_dict(once))

        assert twice == once

    def test_it_is_written_back_in_the_new_shape(self):
        stored = comp.to_dict(comp.from_dict(V1_DOCUMENT))

        assert stored["version"] == comp.VERSION
        assert set(stored) >= {"spine", "layers", "audio"}
        assert "segments" not in stored and "inserts" not in stored


class TestLayersStack:
    def layer(self, name: str, z: int, at: float = 1.0) -> comp.Layer:
        return comp.Layer(source_path=name, at_sec=at, duration_sec=2.0, z=z)

    def test_the_stack_runs_bottom_to_top(self):
        """`overlay` has no z of its own — it composites in the order it is
        applied — so the list order has to be the stack order."""
        composition = build(layers=(
            self.layer("top.mp4", z=9), self.layer("bottom.mp4", z=-1),
        ))

        assert [l.source_path for l in composition.stack] == ["bottom.mp4", "top.mp4"]

    def test_layers_at_one_depth_keep_their_order_in_time(self):
        composition = build(layers=(
            self.layer("late.mp4", z=0, at=8.0), self.layer("early.mp4", z=0, at=2.0),
        ))

        assert [l.source_path for l in composition.stack] == ["early.mp4", "late.mp4"]

    def test_a_negative_z_puts_a_layer_under_the_others(self):
        composition = build(layers=(
            self.layer("a.mp4", z=0), self.layer("backdrop.mp4", z=-5),
        ))

        assert composition.stack[0].source_path == "backdrop.mp4"


class TestAFrameIsARectangle:
    def test_the_default_frame_covers_the_canvas(self):
        assert comp.FULL_FRAME.fills_canvas

    def test_a_frame_in_the_corner_does_not(self):
        assert not comp.Frame(x=75.0, y=10.0, width=40.0).fills_canvas

    def test_a_box_is_whole_even_pixels(self):
        """yuv420p subsamples chroma by two and refuses an odd dimension."""
        left, top, width, height = comp.Frame(width=33.3).box(comp.Canvas())

        assert width % 2 == 0

    def test_a_frame_is_measured_from_its_centre(self):
        left, top, width, height = comp.Frame(
            x=50.0, y=50.0, width=50.0, height=50.0
        ).box(comp.Canvas(1000, 1000, 30))

        assert (left, top, width, height) == (250, 250, 500, 500)

    def test_a_frame_that_leaves_its_height_alone_reports_none(self):
        """Which is what tells the renderer to write `scale=W:-2`."""
        assert comp.Frame(width=40.0).box(comp.Canvas())[3] == 0


class TestALayoutWasAlwaysARectangle:
    """The evidence for removing the enum, printed rather than asserted vaguely.

    `LAYOUT_FILL`, `LAYOUT_BLUR` and `LAYOUT_SPLIT` named three pictures. The
    claim is that a frame and a backdrop name the same three and nothing else
    changed, so each case here computes the geometry the old renderer would
    have produced and checks the frame lands on it exactly.
    """

    CANVAS = comp.Canvas(1080, 1920, 30)

    @staticmethod
    def old_half(canvas: comp.Canvas) -> int:
        """`Canvas.half_height`, as it read before it was deleted.

        Written out here rather than imported: an equivalence proof that asks
        the code under test what the old answer was is not a proof.
        """
        return max(2, (canvas.height // 2) // 2 * 2)

    def test_fill_covered_the_canvas(self):
        """`filters.fill(canvas.width, canvas.height)` — the whole frame."""
        frame = comp.frame_for_layout(comp.LAYOUT_FILL)

        assert frame.box(self.CANVAS) == (0, 0, 1080, 1920)
        assert frame.fit == "cover"

    def test_blur_was_the_canvas_too_with_something_behind_it(self):
        """The foreground was fitted inside the frame — `force_original_aspect
        _ratio=decrease` — which is `contain`, and the backdrop filled the rest."""
        frame = comp.frame_for_layout(comp.LAYOUT_BLUR)

        assert frame.box(self.CANVAS) == (0, 0, 1080, 1920)
        assert frame.fit == "contain"

    def test_split_was_the_top_half_exactly(self):
        """`vstack` of two `fill(width, canvas.half_height)` put the source in
        the upper 1080x960 and the companion directly below it."""
        half = self.old_half(self.CANVAS)
        top = comp.frame_for_layout(comp.LAYOUT_SPLIT)

        assert top.box(self.CANVAS) == (0, 0, 1080, half)

    def test_and_the_companion_was_the_bottom_half_exactly(self):
        half = self.old_half(self.CANVAS)

        assert comp.BOTTOM_HALF.box(self.CANVAS) == (0, half, 1080, half)

    def test_the_two_halves_meet_with_no_seam_and_no_overlap(self):
        """Which is the whole of what `vstack` guaranteed."""
        _, top_y, _, top_h = comp.frame_for_layout(comp.LAYOUT_SPLIT).box(self.CANVAS)
        _, bottom_y, _, bottom_h = comp.BOTTOM_HALF.box(self.CANVAS)

        assert top_y + top_h == bottom_y
        assert bottom_y + bottom_h == self.CANVAS.height

    def test_the_halves_stay_even_on_an_odd_canvas(self):
        """The old `half_height` rounded down to an even number because yuv420p
        subsamples chroma by two; a frame has to do the same or the picture is
        rejected at encode time."""
        odd = comp.Canvas(1080, 1921, 30)

        for frame in (comp.frame_for_layout(comp.LAYOUT_SPLIT), comp.BOTTOM_HALF):
            assert frame.box(odd)[3] % 2 == 0
