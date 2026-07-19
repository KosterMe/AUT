"""The montage model: what a clip is made of, before ffmpeg sees it.

Pure values in, pure values out — which is the point of having the model at
all. Every rule here used to be implicit in the shape of a filter graph.
"""
from __future__ import annotations

import pytest

from app.domain import composition as comp


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
