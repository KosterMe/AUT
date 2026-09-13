"""Profiles: one decision that fixes three others.

What is being guarded here is mostly the *combinations*. A split screen with
b-roll over it as well leaves nothing on screen that is the actual video, and
the two halves of that choice are each individually plausible.

The fourth decision a profile used to fix — whether the source gets
transcribed — is not here any more. It belongs to the cutter, which is the only
party that cannot work without it; see `test_cutting.py`.
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
def test_the_talking_profiles_cut_on_speech(profile):
    assert profile.cutter == cutting.CUTTER_SPEECH


def test_the_film_profile_cuts_on_the_picture():
    """The whole point: an action scene with no dialogue currently produces no
    clips at all."""
    assert profiles.FILM.cutter == cutting.CUTTER_SCENES


def test_no_profile_has_an_opinion_about_transcribing():
    """A profile is a statement about the material and its dressing. Whether
    ASR runs is the cutter's own need, and a profile that could force it made
    the montage side decide what the cutting stage does."""
    assert not hasattr(profiles.FILM, "requires_transcript")


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
            remove_silence=False, inserts=False,
        )


def test_an_unknown_cutter_is_rejected():
    with pytest.raises(ValueError, match="unknown cutter"):
        profiles.Profile(
            name="broken", cutter="vibes", layout=comp.LAYOUT_BLUR,
            remove_silence=False, inserts=False,
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
