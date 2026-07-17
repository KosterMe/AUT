"""Where a video gets cut.

Pure logic over a synthetic transcript, so the cutting rules can be tuned with
fast feedback instead of by rendering an hour of video.
"""
from __future__ import annotations

import pytest

from app.domain.cutting import (
    SliceSpec,
    build_semantic_slices,
    is_sentence_boundary,
    title_from_text,
)
from app.domain.transcript import TranscriptSegment


def segments(*specs: tuple[float, float, str]) -> list[TranscriptSegment]:
    return [TranscriptSegment(start_sec=s, end_sec=e, text=t) for s, e, t in specs]


def speech(duration: float, *, step: float = 5.0, gap: float = 0.1) -> list[TranscriptSegment]:
    """A steady stream of sentences covering `duration` seconds."""
    result = []
    start = 0.0
    index = 0
    while start < duration:
        end = min(start + step, duration)
        result.append(TranscriptSegment(start_sec=start, end_sec=end, text=f"Sentence {index}."))
        start = end + gap
        index += 1
    return result


def test_no_transcript_produces_no_clips():
    assert build_semantic_slices([], source_duration=600, min_clip_seconds=60, max_clip_seconds=90) == []


def test_clips_respect_the_minimum_length():
    specs = build_semantic_slices(
        speech(600), source_duration=600, min_clip_seconds=90, max_clip_seconds=120
    )
    assert specs
    # The last clip is extended to the end of the source, so it can be shorter.
    assert all(spec.duration_sec >= 90 for spec in specs[:-1])


def test_clips_respect_the_maximum_length():
    specs = build_semantic_slices(
        speech(600), source_duration=600, min_clip_seconds=90, max_clip_seconds=120
    )
    # A clip may overshoot slightly to reach a sentence end, but not wildly.
    assert all(spec.duration_sec <= 140 for spec in specs[:-1])


def test_coverage_starts_at_zero_and_reaches_the_end():
    duration = 600.0
    specs = build_semantic_slices(
        speech(duration), source_duration=duration, min_clip_seconds=90, max_clip_seconds=120
    )
    assert specs[0].start_sec == 0.0
    # Nothing after the last spoken word is dropped — outros and credits stay.
    assert specs[-1].end_sec == pytest.approx(duration, abs=0.01)


def test_consecutive_clips_overlap_by_the_gap():
    specs = build_semantic_slices(
        speech(600),
        source_duration=600,
        min_clip_seconds=90,
        max_clip_seconds=120,
        gap_seconds=2.0,
    )
    for current, following in zip(specs, specs[1:]):
        # Overlap, never a hole: a word spoken across the boundary survives.
        assert following.start_sec < current.end_sec
        assert current.end_sec - following.start_sec == pytest.approx(2.0, abs=0.01)


def test_max_clips_caps_the_result():
    specs = build_semantic_slices(
        speech(1200), source_duration=1200, min_clip_seconds=90, max_clip_seconds=120, max_clips=3
    )
    assert len(specs) == 3


def test_indices_are_sequential_from_one():
    specs = build_semantic_slices(
        speech(600), source_duration=600, min_clip_seconds=90, max_clip_seconds=120
    )
    assert [spec.index for spec in specs] == list(range(1, len(specs) + 1))


def test_a_source_shorter_than_one_clip_yields_a_single_clip():
    specs = build_semantic_slices(
        speech(40), source_duration=40, min_clip_seconds=90, max_clip_seconds=120
    )
    assert len(specs) == 1
    assert specs[0].end_sec == pytest.approx(40, abs=0.01)


def test_a_source_shorter_than_the_usable_minimum_yields_nothing():
    specs = build_semantic_slices(
        segments((0.0, 3.0, "Too short.")),
        source_duration=3.0,
        min_clip_seconds=90,
        max_clip_seconds=120,
    )
    assert specs == []


def test_unusable_segments_are_ignored():
    specs = build_semantic_slices(
        segments(
            (0.0, 0.0, "zero length"),
            (10.0, 5.0, "ends before it starts"),
            (0.0, 120.0, "   "),
        ),
        source_duration=120,
        min_clip_seconds=30,
        max_clip_seconds=60,
    )
    assert specs == []


def test_out_of_order_segments_are_sorted_first():
    specs = build_semantic_slices(
        segments(
            (60.0, 120.0, "Second half."),
            (0.0, 60.0, "First half."),
        ),
        source_duration=120,
        min_clip_seconds=30,
        max_clip_seconds=90,
    )
    assert specs[0].start_sec == 0.0
    assert "First half." in specs[0].text


class TestSentenceBoundary:
    def test_a_long_pause_ends_a_clip_regardless_of_punctuation(self):
        assert is_sentence_boundary("no punctuation here", pause_after=1.5)

    def test_terminal_punctuation_ends_a_clip(self):
        assert is_sentence_boundary("A finished thought.", pause_after=0.0)
        assert is_sentence_boundary("Really?!", pause_after=0.0)

    def test_a_short_pause_mid_sentence_does_not(self):
        assert not is_sentence_boundary("and then he", pause_after=0.2)

    def test_a_medium_pause_ends_a_clip_once_enough_words_have_passed(self):
        long_text = " ".join(["word"] * 20)
        assert is_sentence_boundary(long_text, pause_after=0.5)
        assert not is_sentence_boundary("three words only", pause_after=0.5)


class TestTitleFromText:
    def test_uses_the_first_sentence_when_it_is_a_sane_length(self):
        assert title_from_text("This is the hook. And then more.", 1) == "This is the hook."

    def test_truncates_when_there_is_no_early_sentence_break(self):
        title = title_from_text("word " * 60, 2)
        assert len(title) <= 90

    def test_falls_back_to_the_index_for_empty_text(self):
        assert title_from_text("", 7) == "slice 7"


def test_slice_spec_reports_its_duration():
    assert SliceSpec(1, 10.0, 100.5, "t", "x", 3).duration_sec == 90.5
