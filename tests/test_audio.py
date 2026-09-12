"""What a clip sounds like, decided from the library and the timeline.

The placement rule for effects is the part worth guarding: a transition sound
belongs on a moment where the picture already changes, and nowhere else. A
whoosh over a continuous shot is a sound with nothing to explain it.
"""
from __future__ import annotations

import pytest

from app.domain import audio
from app.domain import composition as comp
from app.domain.inserts import AssetOption


def asset(asset_id: int, *tags: str, duration: float = 120.0, still: bool = False, rank: int = 0):
    return AssetOption(
        asset_id=asset_id, path=f"/library/{asset_id}.mp3", tags=tags,
        duration_sec=duration, still=still, last_used_rank=rank,
    )


def clip(*, segments=((0.0, 60.0),), inserts=()) -> comp.Composition:
    return comp.Composition(
        segments=tuple(comp.Segment("/media/src.mp4", start, end) for start, end in segments),
        inserts=tuple(inserts),
    )


def insert_at(at: float) -> comp.Insert:
    return comp.Insert(kind=comp.INSERT_FULL, source_path="/library/b.mp4",
                       at_sec=at, duration_sec=2.0)


# --- the music bed ----------------------------------------------------------


class TestMusic:
    @pytest.mark.parametrize("tag", ["music", "музыка", "муз", "трек"])
    def test_a_track_is_a_bed_whatever_the_library_calls_it(self, tag):
        """A role tag says what a file is for, and the library is tagged in
        whichever language its owner thinks in."""
        assert audio.choose_music(clip(), assets=[asset(1, tag)]) is not None

    def test_a_track_tagged_music_becomes_the_bed(self):
        bed = audio.choose_music(clip(), assets=[asset(1, "music")])

        assert bed is not None
        assert bed.source_path == "/library/1.mp3"

    def test_a_library_without_music_produces_no_bed(self):
        assert audio.choose_music(clip(), assets=[asset(1, "background"), asset(2, "sfx")]) is None

    def test_an_image_is_never_used_as_music(self):
        assert audio.choose_music(clip(), assets=[asset(1, "music", still=True)]) is None

    def test_the_bed_sits_below_the_rest_of_the_mix(self):
        """A bed audible while somebody is talking is too loud, whatever the
        ducking does afterwards."""
        bed = audio.choose_music(clip(), assets=[asset(1, "music")])

        assert bed.gain_db < -6.0

    def test_ducking_pulls_the_music_down_rather_than_up(self):
        bed = audio.choose_music(clip(), assets=[asset(1, "music")])

        assert bed.duck_ratio > 1.0
        assert 0.0 < bed.duck_threshold <= 1.0

    def test_a_long_track_is_entered_at_a_different_point_per_clip(self):
        """Otherwise every clip of a job opens on the same four bars."""
        library = [asset(1, "music", duration=300.0)]

        offsets = {
            audio.choose_music(clip(), assets=library, seed=seed).start_sec
            for seed in range(5)
        }

        assert len(offsets) > 1

    def test_a_track_barely_longer_than_the_clip_starts_at_the_beginning(self):
        bed = audio.choose_music(clip(), assets=[asset(1, "music", duration=60.5)], seed=3)

        assert bed.start_sec == 0.0

    def test_the_choice_is_stable_for_one_clip(self):
        library = [asset(i, "music", duration=300.0) for i in range(1, 5)]

        first = audio.choose_music(clip(), assets=library, seed=7)
        second = audio.choose_music(clip(), assets=list(reversed(library)), seed=7)

        assert first == second


# --- transition sounds ------------------------------------------------------


class TestEffects:
    def test_an_effect_lands_on_every_cut(self):
        composition = clip(segments=((0.0, 20.0), (30.0, 45.0), (60.0, 80.0)))

        effects = audio.choose_effects(composition, assets=[asset(1, "sfx")])

        # Cuts land at 20s and 35s of the output; the sound leads each by 0.12s.
        assert [effect.at_sec for effect in effects] == [19.88, 34.88]

    def test_nothing_fires_at_the_start_of_the_clip(self):
        """There is nothing before the first segment to transition from."""
        effects = audio.choose_effects(clip(), assets=[asset(1, "sfx")])

        assert effects == ()

    def test_an_insert_appearing_is_a_cut_too(self):
        composition = clip(inserts=(insert_at(12.0),))

        effects = audio.choose_effects(composition, assets=[asset(1, "sfx")])

        assert [effect.at_sec for effect in effects] == [11.88]

    def test_effects_keep_their_distance(self):
        composition = clip(inserts=(insert_at(10.0), insert_at(11.0), insert_at(30.0)))

        effects = audio.choose_effects(
            composition, assets=[asset(1, "sfx")],
            policy=audio.AudioPolicy(effect_min_gap_seconds=4.0),
        )

        assert [effect.at_sec for effect in effects] == [9.88, 29.88]

    def test_the_count_is_capped(self):
        composition = clip(inserts=tuple(insert_at(5.0 + i * 5) for i in range(10)))

        effects = audio.choose_effects(
            composition, assets=[asset(1, "sfx")],
            policy=audio.AudioPolicy(max_effects=3, effect_min_gap_seconds=0.0),
        )

        assert len(effects) == 3

    def test_a_library_without_sound_effects_produces_none(self):
        composition = clip(segments=((0.0, 20.0), (30.0, 45.0)))

        assert audio.choose_effects(composition, assets=[asset(1, "music")]) == ()

    @pytest.mark.parametrize("switch,expected", [
        ({"effects_on_cuts": False}, [11.88]),
        ({"effects_on_inserts": False}, [19.88]),
    ])
    def test_either_kind_of_moment_can_be_switched_off(self, switch, expected):
        composition = clip(segments=((0.0, 20.0), (30.0, 45.0)), inserts=(insert_at(12.0),))

        effects = audio.choose_effects(
            composition, assets=[asset(1, "sfx")], policy=audio.AudioPolicy(**switch),
        )

        assert [effect.at_sec for effect in effects] == expected

    def test_a_short_sound_is_not_stretched_and_a_long_one_is_trimmed(self):
        composition = clip(inserts=(insert_at(20.0),))
        policy = audio.AudioPolicy(effect_seconds=1.0)

        short = audio.choose_effects(
            composition, assets=[asset(1, "sfx", duration=0.4)], policy=policy
        )
        long = audio.choose_effects(
            composition, assets=[asset(2, "sfx", duration=30.0)], policy=policy
        )

        assert short[0].duration_sec == 0.4
        assert long[0].duration_sec == 1.0

    def test_the_result_is_a_valid_composition(self):
        composition = clip(segments=((0.0, 20.0), (30.0, 45.0)))

        effects = audio.choose_effects(composition, assets=[asset(1, "sfx")])

        import dataclasses

        assert dataclasses.replace(composition, effects=effects).effects == effects


# --- the model --------------------------------------------------------------


def test_an_effect_past_the_end_of_the_clip_is_rejected():
    with pytest.raises(ValueError, match="past the end"):
        comp.Composition(
            segments=(comp.Segment("/media/src.mp4", 0.0, 10.0),),
            effects=(comp.SoundEffect("/library/w.wav", at_sec=30.0),),
        )


def test_music_that_would_be_boosted_under_speech_is_rejected():
    with pytest.raises(ValueError, match="duck_ratio"):
        comp.MusicBed("/library/track.mp3", duck_ratio=0.5)


def test_a_composition_reports_the_audio_it_brings_of_its_own():
    """The renderer needs this: a silent source with music over it still has to
    produce an audio track."""
    silent = clip()
    scored = comp.Composition(
        segments=silent.segments, music=comp.MusicBed("/library/track.mp3")
    )

    assert silent.has_own_audio is False
    assert scored.has_own_audio is True
    assert "/library/track.mp3" in scored.source_paths
