"""The scenarios that ship with the service.

The one that matters most is the one a job gets when it chose nothing. It has
to reproduce the present montage exactly: a job on the default scenario must
produce a file indistinguishable from the one the pipeline makes today, and
while that holds the conversion has broken nothing.
"""
from __future__ import annotations

import dataclasses

from montage.scenario import model
from montage.style import MUSIC_TAG, StyleSpec


def default(style=None) -> model.Scenario:
    """The present montage, as three elements (§9.1).

        spine       [ source, elastic, fit: auto, pauses removed ]
        background  [ blur_of(source), z: -1 ]
        subtitles   [ karaoke ]

    `fit: auto` is the `choose_layout` rule kept whole: a source already about
    as tall and narrow as the canvas is cropped to fill it, a wider one keeps
    the blurred backdrop. The rule did not disappear, it became the value of a
    field instead of a branch in the compiler.

    The subtitles are the style's, not a track element: this scenario reuses
    `StyleSpec` (see `model`), and `style.subtitles.enabled` is what an editor
    shows as the third row. Switching it off is what §6.1 promises — the
    scenario stops asking for word timings, and Whisper stops running, with no
    second setting to keep in step.
    """
    source = model.Element(
        id="source",
        slot=model.Slot(kind=model.SLOT_SOURCE),
        duration=model.Duration(mode=model.DurationMode.ELASTIC),
        frame=model.Frame(fit=model.FIT_AUTO),
        label="исходник",
    )
    backdrop = model.Element(
        id="backdrop",
        slot=model.Slot(kind=model.SLOT_BLUR_OF, ref="source"),
        duration=model.Duration(mode=model.DurationMode.ELASTIC),
        label="мыльный фон",
    )
    return model.Scenario(
        name="default",
        tracks=(
            model.Track(id="spine", kind=model.TRACK_SPINE, z=0, elements=(source,)),
            model.Track(id="background", kind=model.TRACK_VIDEO, z=-1, elements=(backdrop,)),
        ),
        style=style if style is not None else _with_pauses_removed(),
    )


def _with_pauses_removed() -> StyleSpec:
    """Cutting the pauses out is part of this scenario, not of the defaults.

    §9.1 puts it on the spine element, and with `StyleSpec` reused it lives in
    the pacing group — so the scenario has to state it rather than inherit
    whatever the settings happen to say.
    """
    base = StyleSpec.from_settings()
    return dataclasses.replace(
        base, pacing=dataclasses.replace(base.pacing, remove_silence=True)
    )


def _spine(fit: str = model.FIT_AUTO) -> model.Element:
    """The clip itself, taking whatever length is left. Every scenario has one."""
    return model.Element(
        id="source",
        slot=model.Slot(kind=model.SLOT_SOURCE),
        duration=model.Duration(mode=model.DurationMode.ELASTIC),
        frame=model.Frame(fit=fit),
        label="исходник",
    )


def _backdrop() -> model.Element:
    """The blurred copy behind a source that does not fill the frame."""
    return model.Element(
        id="backdrop",
        slot=model.Slot(kind=model.SLOT_BLUR_OF, ref="source"),
        duration=model.Duration(mode=model.DurationMode.ELASTIC),
        label="мыльный фон",
    )


def _broll(style: StyleSpec) -> model.RuleElement:
    """B-roll on the words that name it.

    The limits come from the style's own insert policy rather than from this
    rule's defaults. They happen to be the same numbers today, and a scenario
    that agreed with the planner by coincidence would stop agreeing the first
    time somebody tuned one of them.
    """
    policy = style.inserts
    return model.RuleElement(
        id="broll",
        rule=model.RULE_KEYWORD_BROLL,
        limit=policy.max_inserts,
        min_gap_sec=policy.min_gap_seconds,
        guard_head_sec=policy.hook_guard_seconds,
        guard_tail_sec=policy.tail_guard_seconds,
        max_share=policy.max_share,
        label="b-roll по ключевым словам",
    )


def _music() -> model.Element:
    """A bed under the clip, ducked out of the way of the speech.

    Picked by the seed rather than by rotation: that is what `choose_music`
    always did, and a job whose clips all opened on the same track would be
    the audible version of the thing rotation exists to prevent.
    """
    return model.Element(
        id="music",
        slot=model.Slot(kind=model.SLOT_LIBRARY, tag=MUSIC_TAG, pick=model.PICK_RANDOM),
        duration=model.Duration(mode=model.DurationMode.ELASTIC),
        audio=model.ElementAudio(enabled=True, loop=True, ducked_by_speech=True),
        label="музыка",
    )


def _stingers() -> model.RuleElement:
    """A sound on every visible change."""
    return model.RuleElement(
        id="cuts", rule=model.RULE_ON_EVERY_CUT, label="звук на склейках",
    )


def _tracks(*, spine, video=(), overlay=(), audio=()) -> tuple[model.Track, ...]:
    made = [model.Track(id="spine", kind=model.TRACK_SPINE, z=0, elements=tuple(spine))]
    if video:
        made.append(model.Track(id="background", kind=model.TRACK_VIDEO, z=-1,
                                elements=tuple(video)))
    if overlay:
        made.append(model.Track(id="over", kind=model.TRACK_OVERLAY, z=1,
                                elements=tuple(overlay)))
    if audio:
        made.append(model.Track(id="sound", kind=model.TRACK_AUDIO, z=0,
                                elements=tuple(audio)))
    return tuple(made)


def _automation(style: StyleSpec) -> tuple[tuple, tuple]:
    """The rules and sounds a style asks for, as overlay and audio elements.

    A built-in scenario is a function of the resolved style, not a fixed list.
    `inserts.enabled`, `audio.music` and `audio.sfx` used to gate the dressing
    pass; here they decide whether the rule is in the scenario at all, which is
    the same answer written where somebody can see it. A job that turned b-roll
    off gets a montage with no b-roll rule rather than a rule that quietly
    declines to fire.
    """
    overlay = []
    audio = []
    if style.inserts.enabled:
        overlay.append(_broll(style))
    if style.audio.enabled and style.audio.sfx:
        overlay.append(_stingers())
    if style.audio.enabled and style.audio.music:
        audio.append(_music())
    return tuple(overlay), tuple(audio)


def talking(style: StyleSpec) -> model.Scenario:
    """Podcasts and interviews: pauses out, b-roll over the talk, music under it."""
    overlay, audio = _automation(style)
    return model.Scenario(
        name="talking", style=style,
        tracks=_tracks(
            spine=(_spine(),), video=(_backdrop(),), overlay=overlay, audio=audio,
        ),
    )


def plain(style: StyleSpec) -> model.Scenario:
    """The same cuts, nothing added and nothing taken out.

    Which is what the style says by default — but a job is free to switch
    something on, and then it is on.
    """
    overlay, audio = _automation(style)
    return model.Scenario(
        name="plain", style=style,
        tracks=_tracks(
            spine=(_spine(),), video=(_backdrop(),), overlay=overlay, audio=audio,
        ),
    )


def split(style: StyleSpec) -> model.Scenario:
    """The speaker on top, filler footage below.

    No b-roll: the bottom half is already carrying the eye, and putting a
    third picture over the two would leave nothing on screen that is the
    actual video.
    """
    overlay, audio = _automation(style)
    return model.Scenario(
        name="split", style=style,
        tracks=_tracks(spine=(_spine(),), overlay=overlay, audio=audio),
    )


def film(style: StyleSpec) -> model.Scenario:
    """Films, shows and sport: the spine and the score it came with."""
    overlay, audio = _automation(style)
    return model.Scenario(
        name="film", style=style,
        tracks=_tracks(
            spine=(_spine(),), video=(_backdrop(),), overlay=overlay, audio=audio,
        ),
    )


BUILTIN = {"talking": talking, "plain": plain, "split": split, "film": film}


def for_profile(name: str, style: StyleSpec) -> model.Scenario:
    """The scenario a named profile became.

    `profiles.py` is a seeder now: the same four decisions, written in the
    model that replaced it. The style carries what a profile used to say about
    pacing, subtitles, inserts and audio (trap 19); the tracks carry what it
    said about what is on screen.
    """
    try:
        return BUILTIN[name](style)
    except KeyError:
        raise ValueError(
            f"unknown scenario {name!r}; expected one of {sorted(BUILTIN)}"
        ) from None
