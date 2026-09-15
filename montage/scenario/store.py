"""A scenario as stored data, and back.

The encoding is mechanical — every node in the tree is a frozen dataclass of
scalars, tuples and other nodes — so it is written once, generically. The
decoding is not. A stored scenario is the only thing standing between an
editor and a render, it outlives the code that wrote it, and it can be
hand-edited; so every node is rebuilt by a function that knows what it is
looking at, missing fields fall back to the model's own defaults, and anything
structurally wrong raises rather than rendering as something else.

That asymmetry is the same one `composition` draws for the same reason, and it
is why there are two obvious-looking halves here of very different lengths.
"""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any, Mapping

from montage.composition import Canvas
from montage.scenario import model
from montage.style import StyleSpec

VERSION = 1


# --- out ---------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """Any node of the tree as JSON-able data."""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


def to_dict(scenario: model.Scenario) -> dict[str, Any]:
    """A scenario as plain data, style and all."""
    return {
        "version": VERSION,
        "name": scenario.name,
        "canvas": _plain(scenario.canvas),
        "mock": _plain(scenario.mock),
        "style": scenario.style.to_dict(),
        "tracks": [
            {
                "id": track.id,
                "kind": track.kind,
                "z": track.z,
                "muted": track.muted,
                "locked": track.locked,
                "elements": [_plain(element) for element in track.elements],
            }
            for track in scenario.tracks
        ],
    }


# --- in ----------------------------------------------------------------------


class MalformedScenario(ValueError):
    """A stored scenario that cannot be trusted to mean anything."""


def _sub(data: Any, key: str) -> Mapping[str, Any] | None:
    value = data.get(key) if isinstance(data, Mapping) else None
    return value if isinstance(value, Mapping) else None


def _event(data: Any) -> model.EventRef | None:
    if not isinstance(data, Mapping):
        return None
    return model.EventRef(
        kind=str(data.get("kind") or "cut"),
        index=int(data.get("index") or 0),
        word=str(data.get("word") or ""),
    )


def _anchor(data: Any) -> model.Anchor | None:
    if not isinstance(data, Mapping):
        return None
    try:
        mode = model.AnchorMode(str(data.get("mode") or "start"))
    except ValueError as exc:
        raise MalformedScenario(f"unknown anchor mode {data.get('mode')!r}") from exc
    return model.Anchor(
        mode=mode,
        value=float(data.get("value") or 0.0),
        event=_event(data.get("event")),
        ref=data.get("ref") or None,
        offset_sec=float(data.get("offset_sec") or 0.0),
    )


def _keyframe(data: Any) -> model.Keyframe:
    at = _anchor(_sub(data, "at")) or model.Anchor()
    return model.Keyframe(
        at=at,
        value=float(data.get("value") or 0.0),
        easing=str(data.get("easing") or "linear"),
    )


def _animated(data: Any, default: float) -> model.Animated:
    if not isinstance(data, Mapping):
        # A bare number is a static value, which is what a hand-written
        # scenario will say and what it plainly means.
        return model.Animated(float(data)) if isinstance(data, (int, float)) else model.Animated(default)
    return model.Animated(
        static=float(data.get("static", default)),
        keys=tuple(_keyframe(key) for key in data.get("keys") or []),
    )


def _rect(data: Any) -> model.Rect | None:
    if not isinstance(data, Mapping):
        return None
    return model.Rect(**{
        name: float(data.get(name, getattr(model.Rect(), name)))
        for name in ("x", "y", "width", "height")
    })


def _frame(data: Any) -> model.Frame:
    if not isinstance(data, Mapping):
        return model.Frame()
    default = model.Frame()
    return model.Frame(
        x=_animated(data.get("x"), default.x.static),
        y=_animated(data.get("y"), default.y.static),
        width=_animated(data.get("width"), default.width.static),
        height=_animated(data.get("height"), default.height.static),
        rotate=_animated(data.get("rotate"), default.rotate.static),
        opacity=_animated(data.get("opacity"), default.opacity.static),
        fit=str(data.get("fit") or default.fit),
        align=str(data.get("align") or default.align),
        radius=float(data.get("radius") or 0.0),
        crop=_rect(data.get("crop")),
        blend=str(data.get("blend") or default.blend),
    )


def _slot(data: Any) -> model.Slot:
    if not isinstance(data, Mapping):
        return model.Slot()
    upload = data.get("upload_id")
    return model.Slot(
        kind=str(data.get("kind") or model.SLOT_SOURCE),
        tag=str(data.get("tag") or ""),
        pick=str(data.get("pick") or model.PICK_ROTATE),
        event=_event(data.get("event")),
        upload_id=int(upload) if upload is not None else None,
        color=str(data.get("color") or ""),
        template=str(data.get("template") or ""),
        ref=str(data.get("ref") or ""),
    )


def _duration(data: Any) -> model.Duration:
    if not isinstance(data, Mapping):
        return model.Duration()
    try:
        mode = model.DurationMode(str(data.get("mode") or "fixed"))
    except ValueError as exc:
        raise MalformedScenario(f"unknown duration mode {data.get('mode')!r}") from exc
    default = model.Duration()
    return model.Duration(
        mode=mode,
        value=float(data.get("value", default.value)),
        grow=float(data.get("grow", default.grow)),
        min_sec=float(data.get("min_sec") or 0.0),
        max_sec=float(data.get("max_sec") or 0.0),
        until=_anchor(data.get("until")),
    )


def _audio(data: Any) -> model.ElementAudio:
    if not isinstance(data, Mapping):
        return model.ElementAudio()
    return model.ElementAudio(
        enabled=bool(data.get("enabled")),
        gain_db=_animated(data.get("gain_db"), 0.0),
        duck_others_db=float(data.get("duck_others_db") or 0.0),
        ducked_by_speech=bool(data.get("ducked_by_speech")),
        fade_in_sec=float(data.get("fade_in_sec") or 0.0),
        fade_out_sec=float(data.get("fade_out_sec") or 0.0),
        loop=bool(data.get("loop")),
    )


def _transition(data: Any) -> model.Transition | None:
    if not isinstance(data, Mapping):
        return None
    return model.Transition(
        kind=str(data.get("kind") or "fade"),
        duration_sec=float(data.get("duration_sec") or 0.4),
    )


def _effects(data: Any) -> tuple[model.Effect, ...]:
    return tuple(
        model.Effect(kind=str(item.get("kind") or ""), params=dict(item.get("params") or {}))
        for item in (data or [])
        if isinstance(item, Mapping) and item.get("kind")
    )


def _element(data: Mapping[str, Any]) -> model.Element:
    return model.Element(
        id=str(data.get("id") or ""),
        slot=_slot(data.get("slot")),
        start=_anchor(data.get("start")) or model.Anchor(),
        duration=_duration(data.get("duration")),
        frame=_frame(data.get("frame")),
        effects=_effects(data.get("effects")),
        audio=_audio(data.get("audio")),
        transition_in=_transition(data.get("transition_in")),
        transition_out=_transition(data.get("transition_out")),
        label=str(data.get("label") or ""),
        optional=bool(data.get("optional")),
        priority=int(data.get("priority") or 0),
    )


def _rule(data: Mapping[str, Any]) -> model.RuleElement:
    template = data.get("template")
    default = model.RuleElement(id="x", rule=model.RULE_KEYWORD_BROLL)
    return model.RuleElement(
        id=str(data.get("id") or ""),
        rule=str(data.get("rule") or ""),
        params=dict(data.get("params") or {}),
        template=_element(template) if isinstance(template, Mapping) else None,
        limit=int(data.get("limit", default.limit)),
        min_gap_sec=float(data.get("min_gap_sec", default.min_gap_sec)),
        guard_head_sec=float(data.get("guard_head_sec", default.guard_head_sec)),
        guard_tail_sec=float(data.get("guard_tail_sec", default.guard_tail_sec)),
        max_share=float(data.get("max_share", default.max_share)),
        label=str(data.get("label") or ""),
    )


def _track(data: Any) -> model.Track:
    if not isinstance(data, Mapping):
        raise MalformedScenario("a track has to be an object")
    elements: list[model.Element | model.RuleElement] = []
    for item in data.get("elements") or []:
        if not isinstance(item, Mapping):
            raise MalformedScenario("an element has to be an object")
        # A rule is an element that says how to make several. The key it
        # carries is the discriminator, because it is the field that makes it
        # one — there is no separate type tag to fall out of step with it.
        elements.append(_rule(item) if item.get("rule") else _element(item))
    return model.Track(
        id=str(data.get("id") or ""),
        kind=str(data.get("kind") or model.TRACK_VIDEO),
        z=int(data.get("z") or 0),
        elements=tuple(elements),
        muted=bool(data.get("muted")),
        locked=bool(data.get("locked")),
    )


def from_dict(data: Mapping[str, Any]) -> model.Scenario:
    """Rebuild a stored scenario.

    Raises `MalformedScenario` rather than guessing. A scenario that cannot be
    read is a bad day for one job; a scenario silently read as a different
    montage is a bad month for every clip rendered from it.
    """
    if not isinstance(data, Mapping):
        raise MalformedScenario("a scenario has to be an object")

    canvas_data = data.get("canvas") or {}
    mock_data = data.get("mock") or {}
    try:
        canvas = Canvas(
            width=int(canvas_data.get("width", 1080)),
            height=int(canvas_data.get("height", 1920)),
            fps=int(canvas_data.get("fps", 30)),
        )
        mock = model.MockClip(
            duration_sec=float(mock_data.get("duration_sec", 90.0)),
            width=int(mock_data.get("width", 1920)),
            height=int(mock_data.get("height", 1080)),
            cuts=tuple(float(at) for at in mock_data.get("cuts") or []),
            sample_clip_id=(
                int(mock_data["sample_clip_id"])
                if mock_data.get("sample_clip_id") is not None else None
            ),
        )
        return model.Scenario(
            name=str(data.get("name") or ""),
            tracks=tuple(_track(track) for track in data.get("tracks") or []),
            canvas=canvas,
            style=StyleSpec.from_dict(data.get("style")),
            mock=mock,
        )
    except MalformedScenario:
        raise
    except (TypeError, ValueError) as exc:
        raise MalformedScenario(str(exc)) from exc
