"""The style: partial in, complete out.

The property every test here is really checking is that nothing is required.
A profile names five fields, a preset names two, a job names one, and what
comes out the other end is a complete description of a clip — because the
moment a layer has to be complete, an unattended pipeline stops being able to
run without somebody filling in a form.
"""
from __future__ import annotations

import pytest

from app.domain import profiles
from app.domain import style as style_module
from app.domain.style import StyleSpec


def test_defaults_are_complete_without_anything_being_asked_for():
    style = style_module.resolve()

    assert style.delivery.width == 1080
    assert style.delivery.height == 1920
    assert style.subtitles.font_size == 82
    assert style.framing.zoom == 1.2
    assert style.grade.sharpen is True


def test_an_override_names_one_field_and_leaves_the_rest():
    """The whole reason a preset can be two lines."""
    style = StyleSpec.from_settings().merged({"subtitles": {"font_size": 110}})

    assert style.subtitles.font_size == 110
    assert style.subtitles.position_percent == 76
    assert style.subtitles.font == "Oswald"
    assert style.framing.zoom == 1.2


def test_none_means_not_asked_for_rather_than_off():
    """A form that sends every control it has, most of them empty, must not
    turn off the things it never showed."""
    style = StyleSpec.from_settings().merged(
        {"inserts": {"enabled": None}, "audio": {"music": None}}
    )

    assert style.inserts.enabled is True
    assert style.audio.music is False  # the default, not the None


def test_unknown_keys_are_ignored_rather_than_rejected():
    """A preset written before a field was renamed still has to load: the
    alternative is a job that fails at three in the morning over a key."""
    style = StyleSpec.from_settings().merged(
        {"subtitles": {"font_size": 96, "colour_scheme": "neon"}, "nonsense": {"x": 1}}
    )

    assert style.subtitles.font_size == 96


def test_values_are_coerced_to_the_field_they_land_in():
    """JSON from a form arrives as strings often enough to matter, and a
    string in a filter graph is a broken render rather than an error."""
    style = StyleSpec.from_settings().merged(
        {"framing": {"zoom": "1.35", "blur_divisor": "2"}, "inserts": {"enabled": "false"}}
    )

    assert style.framing.zoom == pytest.approx(1.35)
    assert style.framing.blur_divisor == 2
    assert style.inserts.enabled is False


def test_numbers_out_of_range_are_clamped_not_refused():
    """A zoom of 12 is a mistake, but failing the clip over it is a worse one."""
    style = StyleSpec.from_settings().merged({"framing": {"zoom": 12.0}})

    assert style.framing.zoom == 2.0


def test_an_unknown_layout_is_refused_because_nothing_can_render_it():
    with pytest.raises(ValueError, match="unknown layout"):
        StyleSpec.from_settings().merged({"framing": {"layout": "hexagon"}})


# --- the resolution chain ---------------------------------------------------


def test_the_profile_decides_what_it_has_a_view_on():
    talking = style_module.resolve(profile=profiles.style_overrides(profiles.TALKING))
    plain = style_module.resolve(profile=profiles.style_overrides(profiles.PLAIN))

    assert talking.pacing.remove_silence is True
    assert talking.inserts.enabled is True
    assert plain.pacing.remove_silence is False
    assert plain.inserts.enabled is False
    # Neither has an opinion about the type, so both get the default.
    assert talking.subtitles.font_size == plain.subtitles.font_size


def test_a_preset_beats_the_profile():
    """The case that a diff-based preset would get wrong: turning something
    off that the profile turns on, when off is also the global default."""
    style = style_module.resolve(
        profile=profiles.style_overrides(profiles.TALKING),
        preset={"pacing": {"remove_silence": False}},
    )

    assert style.pacing.remove_silence is False


def test_the_job_beats_the_preset():
    style = style_module.resolve(
        profile=profiles.style_overrides(profiles.TALKING),
        preset={"subtitles": {"font_size": 100}},
        overrides={"subtitles": {"font_size": 64}},
    )

    assert style.subtitles.font_size == 64


def test_a_broken_layer_costs_the_look_and_not_the_render():
    """Unattended means a preset edited into nonsense must not stop a job."""
    style = style_module.resolve(
        profile=profiles.style_overrides(profiles.TALKING),
        preset={"framing": {"layout": "hexagon"}},
        overrides={"subtitles": {"font_size": 90}},
    )

    assert style.framing.layout == "auto"
    assert style.subtitles.font_size == 90


def test_the_environment_still_sets_the_defaults(configure):
    """An existing .env keeps working; what it no longer does is decide
    anything at render time."""
    configure(AUTOCLIPS_RENDER_FOREGROUND_ZOOM=1.4, AUTOCLIPS_SUBTITLE_FONT_SIZE=64)

    style = style_module.resolve()

    assert style.framing.zoom == pytest.approx(1.4)
    assert style.subtitles.font_size == 64


# --- what gets stored -------------------------------------------------------


def test_sanitise_keeps_a_field_that_matches_the_default():
    """Stored presets are not diffs. `remove_silence: false` has to survive
    even though false is the default, because the profile underneath says
    true and a dropped field would let it win."""
    cleaned = style_module.sanitise({"pacing": {"remove_silence": False}})

    assert cleaned == {"pacing": {"remove_silence": False}}


def test_sanitise_drops_what_nothing_could_read():
    cleaned = style_module.sanitise(
        {"subtitles": {"font": "Impact", "made_up": 1}, "elsewhere": {"x": 2}}
    )

    assert cleaned == {"subtitles": {"font": "Impact"}}


def test_sanitise_refuses_a_style_that_cannot_render():
    with pytest.raises(ValueError):
        style_module.sanitise({"framing": {"layout": "hexagon"}})


def test_a_style_round_trips_through_a_dictionary():
    original = StyleSpec.from_settings().merged(
        {"framing": {"zoom": 1.35}, "subtitles": {"font": "Impact", "uppercase": True}}
    )

    assert StyleSpec.from_dict(original.to_dict()) == original


def test_overrides_over_reports_only_the_difference():
    base = StyleSpec.from_settings()
    changed = base.merged({"grade": {"saturation": 1.3}})

    assert changed.overrides_over(base) == {"grade": {"saturation": 1.3}}
