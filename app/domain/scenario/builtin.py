"""The scenarios that ship with the service.

The one that matters most is the one a job gets when it chose nothing. It has
to reproduce the present montage exactly: a job on the default scenario must
produce a file indistinguishable from the one the pipeline makes today, and
while that holds the conversion has broken nothing.
"""
from __future__ import annotations

import dataclasses

from app.domain.scenario import model
from app.domain.style import StyleSpec


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
