"""Where automatic b-roll is allowed to go.

The rules are the whole feature: nobody reviews these clips before they are
posted, so a guardrail that does not hold produces a video with the hook buried
under stock footage. Pure values in, inserts out — no ffmpeg, no library.
"""
from __future__ import annotations

import pytest

from montage import composition as comp
from montage.rules import inserts
from montage.subtitles import SubtitleCue

CLIP = "/media/source.mp4"


def words(*pairs: tuple[float, str]) -> tuple[SubtitleCue, ...]:
    return tuple(
        SubtitleCue(start_sec=at, end_sec=at + 0.3, text=text) for at, text in pairs
    )


def clip(*, duration: float = 60.0, cues=()) -> comp.Composition:
    return comp.Composition(
        segments=(comp.Segment(CLIP, 0.0, duration),),
        subtitles=comp.SubtitleSpec(cues=tuple(cues)),
    )


def asset(asset_id: int, *tags: str, duration: float = 4.0, still: bool = False, rank: int = 0):
    return inserts.AssetOption(
        asset_id=asset_id, path=f"/library/{asset_id}.mp4", tags=tags,
        duration_sec=duration, still=still, last_used_rank=rank,
    )


# --- matching ---------------------------------------------------------------


@pytest.mark.parametrize("word,tag", [
    ("машина", "машина"),
    ("Машину", "машина"),      # inflection, and case
    ("машине,", "машина"),     # trailing punctuation from the transcript
    ("МАШИНЫ", "машина"),
    ("ёлка", "елка"),          # ё is folded, both directions
])
def test_inflected_words_match_their_tag(word, tag):
    assert inserts.matches(word, tag)


@pytest.mark.parametrize("word,tag", [
    ("столица", "стол"),       # a longer word that merely starts the same
    ("маршрут", "машина"),
    ("bmws", "bmw"),           # short tags are matched exactly, not by stem
    ("", "машина"),
    ("машина", ""),
])
def test_unrelated_words_do_not_match(word, tag):
    assert not inserts.matches(word, tag)


# --- placement --------------------------------------------------------------


def test_an_insert_lands_on_the_word_that_matched_it():
    """The point of using word-level cues: the fragment appears as the word is
    spoken, not somewhere in the vicinity."""
    composition = clip(cues=words((4.0, "быстрая"), (12.0, "машина")))

    chosen = inserts.choose_inserts(composition, assets=[asset(1, "машина")])

    assert len(chosen) == 1
    assert chosen[0].at_sec == 12.0
    assert chosen[0].source_path == "/library/1.mp4"


def test_a_whole_phrase_in_one_cue_still_matches():
    """Transcripts without word timings put a phrase in a single cue. Matching
    only the cue as a whole would silently disable the library on them."""
    composition = clip(cues=(SubtitleCue(start_sec=10.0, end_sec=12.0, text="быстрая машина едет"),))

    chosen = inserts.choose_inserts(composition, assets=[asset(1, "машина")])

    assert [insert.at_sec for insert in chosen] == [10.0]


def test_the_hook_is_never_covered():
    composition = clip(cues=words((1.0, "машина"), (30.0, "машина")))

    chosen = inserts.choose_inserts(
        composition, assets=[asset(1, "машина"), asset(2, "машина")],
        policy=inserts.InsertPolicy(hook_guard_seconds=2.5),
    )

    assert [insert.at_sec for insert in chosen] == [30.0]


def test_nothing_runs_past_the_end_of_the_clip():
    composition = clip(duration=20.0, cues=words((19.0, "машина")))

    assert inserts.choose_inserts(composition, assets=[asset(1, "машина")]) == ()


def test_inserts_keep_their_distance():
    """A passage dense in keywords would otherwise become a slideshow."""
    composition = clip(cues=words((10.0, "машина"), (12.0, "машина"), (25.0, "машина")))
    library = [asset(1, "машина"), asset(2, "машина"), asset(3, "машина")]

    chosen = inserts.choose_inserts(
        composition, assets=library, policy=inserts.InsertPolicy(min_gap_seconds=6.0)
    )

    assert [insert.at_sec for insert in chosen] == [10.0, 25.0]


def test_coverage_is_capped_as_a_share_of_the_clip():
    composition = clip(duration=40.0, cues=words(*[(6.0 + i * 7, "машина") for i in range(5)]))
    library = [asset(i, "машина", rank=i) for i in range(1, 6)]

    chosen = inserts.choose_inserts(
        composition, assets=library,
        policy=inserts.InsertPolicy(max_share=0.2, min_gap_seconds=1.0, max_inserts=10),
    )

    assert sum(insert.duration_sec for insert in chosen) <= 40.0 * 0.2


def test_one_asset_does_not_appear_twice_in_the_same_clip():
    """With fewer fragments than moments, the extra moments stay uncovered
    rather than repeating footage."""
    composition = clip(cues=words((10.0, "машина"), (30.0, "машина"), (50.0, "машина")))
    library = [asset(1, "машина"), asset(2, "машина")]

    chosen = inserts.choose_inserts(composition, assets=library)

    assert len(chosen) == 2
    assert len({insert.source_path for insert in chosen}) == 2


def test_the_least_recently_used_asset_goes_first():
    composition = clip(cues=words((10.0, "машина")))
    library = [asset(1, "машина", rank=5), asset(2, "машина", rank=0)]

    chosen = inserts.choose_inserts(composition, assets=library)

    assert chosen[0].source_path == "/library/2.mp4"


def test_a_more_specific_tag_wins_the_moment():
    composition = clip(cues=words((10.0, "феррари")))
    library = [asset(1, "авто"), asset(2, "феррари")]

    chosen = inserts.choose_inserts(composition, assets=library)

    assert chosen[0].source_path == "/library/2.mp4"


# --- durations --------------------------------------------------------------


def test_a_short_video_plays_out_instead_of_being_cut():
    composition = clip(cues=words((10.0, "машина")))

    chosen = inserts.choose_inserts(
        composition, assets=[asset(1, "машина", duration=2.0)],
        policy=inserts.InsertPolicy(min_seconds=1.5, max_seconds=3.5),
    )

    assert chosen[0].duration_sec == 2.0


def test_a_long_video_is_trimmed_to_the_ceiling():
    composition = clip(cues=words((10.0, "машина")))

    chosen = inserts.choose_inserts(
        composition, assets=[asset(1, "машина", duration=90.0)],
        policy=inserts.InsertPolicy(max_seconds=3.5),
    )

    assert chosen[0].duration_sec == 3.5


def test_a_still_is_held_for_the_full_length_and_marked_as_one():
    """A still has no timeline, so the compiler has to loop it rather than
    play it — which it only knows from this flag."""
    composition = clip(cues=words((10.0, "машина")))

    chosen = inserts.choose_inserts(
        composition, assets=[asset(1, "машина", duration=0.0, still=True)],
        policy=inserts.InsertPolicy(max_seconds=3.0),
    )

    assert chosen[0].still is True
    assert chosen[0].duration_sec == 3.0


# --- fallbacks --------------------------------------------------------------


def test_a_clip_that_matches_nothing_gets_nothing():
    """The default, and the point of tagging at all.

    With the fixed-beat fallback on, a library lands on every clip whatever it
    is about: a QR code tagged "машина, мусор" went onto twenty consecutive
    clips of a podcast that mentioned neither. Tags decide, or they decide
    nothing."""
    composition = clip(duration=40.0, cues=words((10.0, "погода"), (20.0, "разговор")))
    library = [asset(1, "машина"), asset(2, "самолёт"), asset(3, "город")]

    assert inserts.choose_inserts(composition, assets=library) == ()


def test_the_fixed_beat_fallback_still_exists_for_a_library_of_filler():
    composition = clip(duration=40.0, cues=words((10.0, "погода"), (20.0, "разговор")))
    library = [asset(1, "машина"), asset(2, "самолёт"), asset(3, "город")]

    chosen = inserts.choose_inserts(
        composition,
        assets=library,
        policy=inserts.InsertPolicy(cadence_seconds=12.0, cadence_when_no_match=True),
    )

    assert [insert.at_sec for insert in chosen] == [2.5, 14.5, 26.5]


def test_a_library_of_nothing_but_audio_produces_nothing():
    """An mp3 on screen is a black frame; the planner must not reach for one
    however well its tags match."""
    composition = clip(cues=words((10.0, "машина")))
    track = inserts.AssetOption(asset_id=1, path="/library/1.mp3", tags=("машина",), audio=True)

    assert inserts.choose_inserts(composition, assets=[track]) == ()


def test_audio_assets_are_skipped_in_favour_of_video_ones():
    composition = clip(cues=words((10.0, "машина")))
    track = inserts.AssetOption(asset_id=1, path="/library/1.mp3", tags=("машина",), audio=True)

    chosen = inserts.choose_inserts(composition, assets=[track, asset(2, "машина")])

    assert [insert.source_path for insert in chosen] == ["/library/2.mp4"]


def test_an_empty_library_produces_nothing():
    assert inserts.choose_inserts(clip(cues=words((10.0, "машина"))), assets=[]) == ()


def test_a_clip_without_subtitles_gets_no_broll():
    """No cues is no words, and words are the only thing that places b-roll.

    Turning subtitles off used to turn the whole library on instead."""
    composition = comp.Composition(segments=(comp.Segment(CLIP, 0.0, 30.0),))

    assert inserts.choose_inserts(composition, assets=[asset(1, "машина")]) == ()


def test_a_clip_without_subtitles_takes_the_beat_when_it_is_asked_for():
    composition = comp.Composition(segments=(comp.Segment(CLIP, 0.0, 30.0),))

    chosen = inserts.choose_inserts(
        composition,
        assets=[asset(1, "машина")],
        policy=inserts.InsertPolicy(cadence_when_no_match=True),
    )

    assert len(chosen) == 1


# --- determinism ------------------------------------------------------------


def test_the_same_clip_is_edited_the_same_way_twice():
    """A re-render that shuffles its own b-roll would invalidate the fragment
    cache and make every comparison between renders meaningless."""
    composition = clip(cues=words((10.0, "машина"), (30.0, "машина")))
    library = [asset(i, "машина") for i in range(1, 6)]

    first = inserts.choose_inserts(composition, assets=library, seed=42)
    second = inserts.choose_inserts(composition, assets=list(reversed(library)), seed=42)

    assert first == second


def test_the_result_is_a_valid_composition():
    """The planner's output has to survive the model's own validation — an
    insert past the end of the clip would be rejected at render time."""
    composition = clip(cues=words((10.0, "машина"), (30.0, "машина")))

    chosen = inserts.choose_inserts(composition, assets=[asset(1, "машина"), asset(2, "машина")])

    import dataclasses

    assert dataclasses.replace(composition, inserts=chosen).inserts == chosen
