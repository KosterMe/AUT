"""The montage model: what a clip is made of, before ffmpeg sees it.

Pure values in, pure values out — which is the point of having the model at
all. Every rule here used to be implicit in the shape of a filter graph.
"""
from __future__ import annotations

import pytest

from montage import composition as comp
from montage.subtitles import SubtitleCue


def test_single_source_without_keep_segments_is_one_continuous_cut():
    result = comp.single_source("a.mp4", start_sec=90.0, end_sec=120.0)

    assert len(result.segments) == 1
    assert result.segments[0].source_start_sec == 90.0
    assert result.segments[0].source_end_sec == 120.0
    assert result.duration_sec == 30.0


def test_keep_segments_are_relative_to_the_clip_start():
    """Silence detection reports windows inside the clip, not inside the file.

    Reading them as absolute source times would cut from the wrong place —
    a clip starting at minute nine would render the first nine minutes.
    """
    result = comp.single_source(
        "a.mp4", start_sec=540.0, end_sec=580.0, keep_segments=[(0.0, 12.0), (18.0, 40.0)]
    )

    assert [(s.source_start_sec, s.source_end_sec) for s in result.segments] == [
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
    assert len(result.segments) == 1
    assert result.segments[0].source_start_sec == 1.0

    empty = comp.single_source("a.mp4", start_sec=0.0, end_sec=10.0, keep_segments=[(0.0, 0.001)])
    assert len(empty.segments) == 1


def test_source_paths_are_deduplicated_in_first_use_order():
    result = comp.Composition(
        segments=(
            comp.Segment("a.mp4", 0.0, 5.0),
            comp.Segment("a.mp4", 10.0, 15.0),
            comp.Segment("b.mp4", 0.0, 5.0, layout=comp.LAYOUT_FILL),
        ),
        inserts=(comp.Insert(comp.INSERT_PIP, "c.mp4", at_sec=1.0, duration_sec=2.0),),
    )

    assert result.source_paths == ("a.mp4", "b.mp4", "c.mp4")


def test_an_insert_cannot_run_past_the_end_of_the_clip():
    with pytest.raises(ValueError, match="past the end"):
        comp.Composition(
            segments=(comp.Segment("a.mp4", 0.0, 10.0),),
            inserts=(comp.Insert(comp.INSERT_FULL, "b.mp4", at_sec=9.0, duration_sec=3.0),),
        )


def test_a_split_screen_segment_needs_a_companion():
    with pytest.raises(ValueError, match="companion"):
        comp.Segment("a.mp4", 0.0, 10.0, layout=comp.LAYOUT_SPLIT)


def test_a_composition_needs_at_least_one_segment():
    with pytest.raises(ValueError, match="at least one segment"):
        comp.Composition(segments=())


def test_a_segment_shorter_than_the_floor_is_rejected():
    with pytest.raises(ValueError, match="floor"):
        comp.Segment("a.mp4", 5.0, 5.01)


def test_unknown_layouts_and_insert_kinds_are_rejected():
    with pytest.raises(ValueError, match="unknown layout"):
        comp.Segment("a.mp4", 0.0, 5.0, layout="ken-burns")
    with pytest.raises(ValueError, match="unknown insert kind"):
        comp.Insert("explosion", "b.mp4", at_sec=0.0, duration_sec=1.0)


# --- storing a composition --------------------------------------------------


def test_a_composition_round_trips_through_json():
    """What makes a finished clip inspectable, and re-renderable without
    re-deciding anything: everything that shaped it survives the trip."""
    original = comp.Composition(
        segments=(
            comp.Segment("/media/a.mp4", 10.0, 20.0),
            comp.Segment("/media/a.mp4", 40.0, 45.0, layout=comp.LAYOUT_FILL),
        ),
        inserts=(comp.Insert(comp.INSERT_FULL, "/media/broll.mp4", at_sec=4.0, duration_sec=2.0),),
        subtitles=comp.SubtitleSpec(
            cues=(SubtitleCue(start_sec=1.0, end_sec=1.4, text="привет"),),
            title_text="часть 1",
        ),
        music=comp.MusicBed("/media/bed.mp3", gain_db=-18.0),
        effects=(comp.SoundEffect("/media/whoosh.wav", at_sec=9.9),),
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
        segments=(
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

    assert len(window.segments) == 1
    assert window.segments[0].source_start_sec == 202.0
    assert window.segments[0].source_end_sec == 206.0
    assert window.duration_sec == 4.0


def test_an_excerpt_spanning_a_cut_keeps_both_sides_of_it():
    window = comp.excerpt(build(), at_sec=8.0, duration_sec=4.0)

    assert [(s.source_start_sec, s.source_end_sec) for s in window.segments] == [
        (108.0, 110.0), (200.0, 202.0)
    ]


def test_an_excerpt_moves_everything_laid_over_the_clip_with_it():
    composition = build(
        inserts=(comp.Insert(comp.INSERT_FULL, "/media/b.mp4", at_sec=11.0, duration_sec=2.0),),
        subtitles=comp.SubtitleSpec(
            cues=(
                SubtitleCue(start_sec=2.0, end_sec=2.4, text="early"),
                SubtitleCue(start_sec=11.5, end_sec=11.9, text="inside"),
            ),
        ),
        effects=(comp.SoundEffect("/media/w.wav", at_sec=11.2),),
    )

    window = comp.excerpt(composition, at_sec=10.0, duration_sec=4.0)

    assert window.inserts[0].at_sec == 1.0
    assert [cue.text for cue in window.subtitles.cues] == ["inside"]
    assert window.subtitles.cues[0].start_sec == 1.5
    assert window.effects[0].at_sec == pytest.approx(1.2)


def test_an_excerpt_advances_the_music_rather_than_restarting_it():
    """A preview of the last ten seconds should hear the part of the track
    that plays there."""
    window = comp.excerpt(build(music=comp.MusicBed("/media/bed.mp3")), at_sec=12.0,
                          duration_sec=4.0)

    assert window.music.start_sec == 12.0


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
