"""The cutters that do not read a transcript.

Both exist for material the speech cutter cannot touch: a film with no
dialogue, a music edit, anything where the only signal is the picture or the
clock. The speech cutter has its own file.
"""
from __future__ import annotations

import pytest

from app.domain import cutting


def spans(specs):
    return [(spec.start_sec, spec.end_sec) for spec in specs]


# --- by the clock -----------------------------------------------------------


class TestPlain:
    def test_it_covers_the_whole_source(self):
        specs = cutting.build_plain_slices(source_duration=300.0, clip_seconds=90.0)

        assert specs[0].start_sec == 0.0
        assert specs[-1].end_sec == 300.0

    def test_clips_overlap_by_the_gap(self):
        specs = cutting.build_plain_slices(
            source_duration=300.0, clip_seconds=90.0, gap_seconds=1.0
        )

        assert specs[1].start_sec == specs[0].end_sec - 1.0

    def test_a_stub_at_the_end_is_absorbed_rather_than_published(self):
        """A four-second final part is not a clip, it is a mistake."""
        specs = cutting.build_plain_slices(source_duration=93.0, clip_seconds=90.0)

        assert spans(specs) == [(0.0, 93.0)]

    def test_a_source_too_short_to_publish_produces_nothing(self):
        assert cutting.build_plain_slices(source_duration=3.0, clip_seconds=90.0) == []

    def test_max_clips_stops_it_early(self):
        specs = cutting.build_plain_slices(
            source_duration=3600.0, clip_seconds=60.0, max_clips=3
        )

        assert len(specs) == 3
        assert [spec.index for spec in specs] == [1, 2, 3]


# --- by the picture ---------------------------------------------------------


class TestScenes:
    def test_a_clip_ends_at_the_first_cut_past_the_minimum(self):
        specs = cutting.build_scene_slices(
            source_duration=200.0,
            scene_changes=[20.0, 95.0, 112.0, 150.0],
            min_clip_seconds=90.0,
            max_clip_seconds=120.0,
            gap_seconds=0.0,
        )

        # 20 is too early to end the first clip; 95 is the first one that is
        # long enough, and cutting there is invisible because the picture cuts.
        assert specs[0].end_sec == 95.0

    def test_a_long_take_is_cut_at_the_maximum_anyway(self):
        """A slightly awkward cut beats a clip that never ends."""
        specs = cutting.build_scene_slices(
            source_duration=300.0,
            scene_changes=[],
            min_clip_seconds=90.0,
            max_clip_seconds=120.0,
            gap_seconds=0.0,
        )

        assert specs[0].end_sec == 120.0

    def test_cuts_outside_the_source_are_ignored(self):
        specs = cutting.build_scene_slices(
            source_duration=200.0,
            scene_changes=[-5.0, 0.0, 95.0, 900.0],
            min_clip_seconds=90.0,
            max_clip_seconds=120.0,
            gap_seconds=0.0,
        )

        assert specs[0].end_sec == 95.0

    def test_the_tail_is_absorbed_into_the_last_clip(self):
        specs = cutting.build_scene_slices(
            source_duration=100.0,
            scene_changes=[97.0],
            min_clip_seconds=90.0,
            max_clip_seconds=120.0,
        )

        assert spans(specs) == [(0.0, 100.0)]

    def test_the_loudest_stretches_are_the_ones_kept(self):
        """When only a handful of clips are wanted out of a two-hour film, the
        loud ones are the ones worth having."""
        loudness = [(float(second), -50.0) for second in range(600)]
        loudness[400:500] = [(float(second), -8.0) for second in range(400, 500)]

        specs = cutting.build_scene_slices(
            source_duration=600.0,
            scene_changes=[float(at) for at in range(100, 600, 100)],
            loudness=loudness,
            min_clip_seconds=90.0,
            max_clip_seconds=120.0,
            gap_seconds=0.0,
            max_clips=1,
        )

        assert len(specs) == 1
        assert specs[0].start_sec == 400.0

    def test_kept_clips_stay_in_order_and_are_renumbered(self):
        loudness = [(float(second), -60.0 + second % 30) for second in range(600)]

        specs = cutting.build_scene_slices(
            source_duration=600.0,
            scene_changes=[float(at) for at in range(100, 600, 100)],
            loudness=loudness,
            min_clip_seconds=90.0,
            max_clip_seconds=120.0,
            max_clips=3,
        )

        assert [spec.index for spec in specs] == [1, 2, 3]
        assert specs == sorted(specs, key=lambda spec: spec.start_sec)

    def test_an_unmeasured_clip_ranks_below_a_measured_quiet_one(self):
        """Silence is about -70 dB; a stretch nothing was measured for must not
        win by default."""
        quiet = cutting.SliceSpec(1, 0.0, 90.0, "", "", 0)
        unknown = cutting.SliceSpec(2, 200.0, 290.0, "", "", 0)
        loudness = [(float(second), -68.0) for second in range(90)]

        assert cutting.loudness_of(quiet, loudness) > cutting.loudness_of(unknown, loudness)

    def test_a_source_too_short_to_publish_produces_nothing(self):
        assert cutting.build_scene_slices(
            source_duration=2.0, scene_changes=[1.0],
            min_clip_seconds=90.0, max_clip_seconds=120.0,
        ) == []


# --- the shared contract ----------------------------------------------------


@pytest.mark.parametrize("build", [
    lambda: cutting.build_plain_slices(source_duration=300.0, clip_seconds=90.0),
    lambda: cutting.build_scene_slices(
        source_duration=300.0, scene_changes=[95.0, 190.0],
        min_clip_seconds=90.0, max_clip_seconds=120.0,
    ),
])
def test_every_cutter_numbers_from_one_and_moves_forward(build):
    specs = build()

    assert [spec.index for spec in specs] == list(range(1, len(specs) + 1))
    assert all(spec.duration_sec > 0 for spec in specs)
    assert all(
        later.start_sec >= earlier.start_sec
        for earlier, later in zip(specs, specs[1:])
    )
