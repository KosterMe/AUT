"""The values that cross the wire, written down once for both ends.

One encoder and one decoder per value, used by the client to send and by the
service to receive. Two copies of this — one per side — is the classic way for
a request to mean one thing on the way out and another on the way in, and the
classic place for it to be discovered is production.

Nothing here is clever: every value already knows how to be data
(`composition.to_dict`, `StyleSpec.to_dict`, `scenario.store.to_dict`), and
what is left is the handful of dataclasses that did not need to until now.
"""
from __future__ import annotations

from typing import Any, Mapping

from montage import composition as comp
from montage import style as style_module
from montage.rules.inserts import AssetOption
from montage.scenario import store


def asset_to_dict(asset: AssetOption) -> dict[str, Any]:
    return {
        "asset_id": asset.asset_id,
        "path": asset.path,
        "tags": list(asset.tags),
        "duration_sec": asset.duration_sec,
        "still": asset.still,
        "audio": asset.audio,
        "last_used_rank": asset.last_used_rank,
    }


def asset_from_dict(data: Mapping[str, Any]) -> AssetOption:
    return AssetOption(
        asset_id=int(data.get("asset_id") or 0),
        path=str(data.get("path") or ""),
        tags=tuple(str(tag) for tag in data.get("tags") or ()),
        duration_sec=float(data.get("duration_sec") or 0.0),
        still=bool(data.get("still")),
        audio=bool(data.get("audio")),
        last_used_rank=int(data.get("last_used_rank") or 0),
    )


def library_to_dict(library) -> dict[str, Any]:
    return {
        "options": [asset_to_dict(option) for option in library.options],
        "companion_path": library.companion_path,
    }


def library_from_dict(data: Mapping[str, Any] | None):
    from montage.client import Library

    if not isinstance(data, Mapping):
        return Library()
    return Library(
        options=[
            asset_from_dict(item) for item in data.get("options") or []
            if isinstance(item, Mapping)
        ],
        companion_path=data.get("companion_path") or None,
    )


def clip_request_to_dict(request) -> dict[str, Any]:
    return {
        "source_path": request.source_path,
        "start_sec": request.start_sec,
        "end_sec": request.end_sec,
        "style": request.style.to_dict(),
        # The words arrive as data because they are a fact AUT measured, and
        # the service has no way to produce them (§3.4). They are whatever
        # shape the transcript had; the subtitle builder is generous about it.
        "speech": list(request.speech),
        "fallback_text": request.fallback_text,
        "title_text": request.title_text,
        "library": library_to_dict(request.library),
        "seed": request.seed,
        "index": request.index,
        "scenario": None if request.scenario is None else store.to_dict(request.scenario),
    }


def clip_request_from_dict(data: Mapping[str, Any]):
    from montage.client import ClipRequest

    scenario = data.get("scenario")
    return ClipRequest(
        source_path=str(data.get("source_path") or ""),
        start_sec=float(data.get("start_sec") or 0.0),
        end_sec=float(data.get("end_sec") or 0.0),
        style=style_module.StyleSpec.from_dict(data.get("style")),
        speech=tuple(data.get("speech") or ()),
        fallback_text=str(data.get("fallback_text") or ""),
        title_text=str(data.get("title_text") or ""),
        library=library_from_dict(data.get("library")),
        seed=int(data.get("seed") or 0),
        index=int(data.get("index") or 1),
        scenario=store.from_dict(scenario) if isinstance(scenario, Mapping) else None,
    )


def clip_plan_to_dict(plan) -> dict[str, Any]:
    return {
        "composition": comp.to_dict(plan.composition),
        "companion_path": plan.companion_path,
        "notes": list(plan.notes),
    }


def clip_plan_from_dict(data: Mapping[str, Any]):
    from montage.client import ClipPlan

    return ClipPlan(
        composition=comp.from_dict(data.get("composition") or {}),
        companion_path=data.get("companion_path") or None,
        notes=tuple(str(note) for note in data.get("notes") or ()),
    )


def render_result_to_dict(result) -> dict[str, Any]:
    return {
        "output_path": result.output_path,
        "output_duration": result.output_duration,
        "segment_count": result.segment_count,
        "subtitles_path": result.subtitles_path,
        "subtitle_count": result.subtitle_count,
        "silence_removed_seconds": result.silence_removed_seconds,
        "strategy": result.strategy,
        "fragments_reused": result.fragments_reused,
        "qa": dict(result.qa or {}),
    }


def render_result_from_dict(data: Mapping[str, Any]):
    from montage.render.compiler import ClipRenderResult

    return ClipRenderResult(
        output_path=str(data.get("output_path") or ""),
        output_duration=float(data.get("output_duration") or 0.0),
        segment_count=int(data.get("segment_count") or 0),
        subtitles_path=data.get("subtitles_path") or None,
        subtitle_count=int(data.get("subtitle_count") or 0),
        silence_removed_seconds=float(data.get("silence_removed_seconds") or 0.0),
        strategy=str(data.get("strategy") or ""),
        fragments_reused=int(data.get("fragments_reused") or 0),
        qa=dict(data.get("qa") or {}),
    )
