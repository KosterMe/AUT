"""Profiles: one decision that fixes four others.

What is being guarded here is mostly the *combinations*. A profile that cuts on
scene changes while insisting on a transcript, or a split screen with b-roll
over it as well, are each individually plausible and together produce a clip
nobody wants.
"""
from __future__ import annotations

import pytest

from app.domain import composition as comp
from app.domain import cutting, profiles


def test_the_default_is_the_podcast_profile():
    assert profiles.get(None).name == "talking"
    assert profiles.get("") is profiles.TALKING


def test_names_are_matched_loosely():
    assert profiles.get(" FILM ") is profiles.FILM


def test_an_unknown_profile_says_what_the_choices_are():
    with pytest.raises(ValueError, match="expected one of"):
        profiles.get("podcast")


@pytest.mark.parametrize("profile", [profiles.TALKING, profiles.PLAIN, profiles.SPLIT])
def test_speech_profiles_need_a_transcript(profile):
    assert profile.cutter == cutting.CUTTER_SPEECH
    assert profile.requires_transcript is True


def test_the_film_profile_runs_without_one():
    """The whole point: an action scene with no dialogue currently produces no
    clips at all."""
    assert profiles.FILM.cutter == cutting.CUTTER_SCENES
    assert profiles.FILM.requires_transcript is False


def test_a_split_screen_carries_no_broll():
    """The bottom half is already carrying the eye; b-roll over it as well
    leaves nothing on screen that is the actual video."""
    assert profiles.SPLIT.layout == comp.LAYOUT_SPLIT
    assert profiles.SPLIT.inserts is False
    assert profiles.SPLIT.companion_tag == profiles.BACKGROUND_TAG


@pytest.mark.parametrize("profile", [profiles.TALKING, profiles.PLAIN, profiles.FILM])
def test_every_profile_but_split_lets_the_source_decide_the_frame(profile):
    """Pinning these to "blur" is how the fill layout stayed unreachable: it
    exists in the compiler, and nothing ever asked for it."""
    assert profile.layout == comp.LAYOUT_AUTO


def test_plain_takes_nothing_out_and_adds_nothing():
    assert profiles.PLAIN.remove_silence is False
    assert profiles.PLAIN.inserts is False


def test_a_split_profile_without_a_companion_tag_is_rejected():
    with pytest.raises(ValueError, match="companion tag"):
        profiles.Profile(
            name="broken", cutter=cutting.CUTTER_SPEECH, layout=comp.LAYOUT_SPLIT,
            requires_transcript=True, remove_silence=False, inserts=False,
        )


def test_an_unknown_cutter_is_rejected():
    with pytest.raises(ValueError, match="unknown cutter"):
        profiles.Profile(
            name="broken", cutter="vibes", layout=comp.LAYOUT_BLUR,
            requires_transcript=True, remove_silence=False, inserts=False,
        )


# --- overrides --------------------------------------------------------------


def test_resolving_a_profile_yields_its_decisions():
    settings = profiles.resolve(profiles.TALKING)

    assert settings["cutter"] == cutting.CUTTER_SPEECH
    assert settings["inserts"] is True
    assert settings["auto_montage"] is True


def test_an_explicit_override_wins():
    settings = profiles.resolve(profiles.TALKING, {"inserts": False, "auto_montage": False})

    assert settings["inserts"] is False
    assert settings["auto_montage"] is False


def test_an_unmentioned_switch_keeps_the_profile_value():
    """`None` means "not asked for". Without that distinction a request that
    simply did not mention b-roll would turn it off."""
    settings = profiles.resolve(profiles.TALKING, {"inserts": None, "crf": 20})

    assert settings["inserts"] is True


def test_overrides_cannot_invent_new_keys():
    settings = profiles.resolve(profiles.TALKING, {"nonsense": True})

    assert "nonsense" not in settings
