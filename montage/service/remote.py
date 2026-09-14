"""The other end of the wire: the same calls, over HTTP.

Function for function with the impure half of `montage.client` — the calls
that touch files or burn CPU, which are exactly the ones worth moving to where
the files and the CPU are. The pure half (building a scenario, trimming a
composition, asking what a montage needs to know) stays local, because sending
arithmetic across a network to have it done is not a service, it is latency.

One implementation of each *value* is shared with the door through
`service.wire`, which is what keeps a request meaning the same thing on both
ends. What is left here is transport: a URL, a token, and two timeouts —
because a render is minutes and a probe is milliseconds, and one number for
both would be either a hair trigger or no protection at all.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from montage import composition as comp
from montage.config import get_settings
from montage.service import wire

log = logging.getLogger(__name__)


class MontageServiceError(RuntimeError):
    """The service refused or could not be reached.

    One exception for both, deliberately: from AUT's side "the montage service
    said no" and "the montage service did not answer" need the same handling —
    the clip is not rendered and the task has to fail or retry. Which of the
    two it was belongs in the message, not in the type.
    """


def configured() -> str:
    """The service's URL, or empty when the montage runs in this process."""
    return get_settings().service.url.strip().rstrip("/")


def _call(
    method: str, path: str, *, json: Any = None, timeout: float | None = None
) -> Any:
    settings = get_settings().service
    url = f"{configured()}{path}"
    headers = {"Authorization": f"Bearer {settings.token}"} if settings.token else {}
    try:
        response = httpx.request(
            method, url, json=json, headers=headers,
            timeout=timeout or settings.timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise MontageServiceError(f"montage service at {url} did not answer: {exc}") from exc
    if response.status_code >= 400:
        detail = _detail(response)
        raise MontageServiceError(f"montage service refused {path}: {detail}")
    return response.json()


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"{response.status_code} {response.text[:200]}"
    if isinstance(body, dict) and "detail" in body:
        return f"{response.status_code} {body['detail']}"
    return f"{response.status_code} {str(body)[:200]}"


# --- the calls, in the order `client` declares them --------------------------


def compose(request):
    return wire.clip_plan_from_dict(
        _call("POST", "/compose", json={"request": wire.clip_request_to_dict(request)})
    )


def render(
    composition: comp.Composition,
    output_path: str,
    *,
    strategy: str | None = None,
    source_duration_sec: float = 0.0,
    on_progress=None,
):
    # `on_progress` is not sent: progress is a callback, and a callback does
    # not fit through a request. The service logs its own progress; AUT's
    # task reports "rendering" and waits, which is what it did anyway.
    return wire.render_result_from_dict(_call(
        "POST", "/render",
        json={
            "composition": comp.to_dict(composition),
            "output_path": output_path,
            "strategy": strategy,
            "source_duration_sec": source_duration_sec,
        },
        timeout=get_settings().service.render_timeout_seconds,
    ))


def preview(composition: comp.Composition, output_path: str, *, spec=None):
    from montage.render.compiler import PreviewSpec

    window = spec or PreviewSpec()
    return wire.render_result_from_dict(_call(
        "POST", "/preview",
        json={
            "composition": comp.to_dict(composition),
            "output_path": output_path,
            "at_sec": window.at_sec,
            "duration_sec": window.duration_sec,
            "scale": window.scale,
            "crf": window.crf,
        },
        timeout=get_settings().service.render_timeout_seconds,
    ))


def cover(
    *,
    source_path: str,
    output_path: str,
    start_sec: float,
    title_text: str,
    part_text: str,
    thumbnail_path: str | None,
    width: int,
    height: int,
) -> str | None:
    body = _call("POST", "/cover", json={
        "source_path": source_path,
        "output_path": output_path,
        "start_sec": start_sec,
        "title_text": title_text,
        "part_text": part_text,
        "thumbnail_path": thumbnail_path,
        "width": width,
        "height": height,
    }, timeout=get_settings().service.render_timeout_seconds)
    return body.get("path")


def analyse(source_path: str) -> dict[str, Any]:
    return _call("POST", "/analyse", json={"source_path": source_path})


def has_audio(source_path: str) -> bool:
    return bool(_call("POST", "/has-audio", json={"source_path": source_path})["has_audio"])


def renderer_available() -> str | None:
    return _call("GET", "/health").get("ffmpeg")


def capabilities() -> dict[str, Any]:
    return _call("GET", "/capabilities")


def fragment_cache() -> tuple[Path, str]:
    body = _call("GET", "/fragment-cache")
    return Path(body["directory"]), body["suffix"]
