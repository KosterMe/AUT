"""The montage service over HTTP: §2.3's contract, as far as AUT needs it.

Every endpoint here is two lines of work and a lot of care about what crosses:
read the values, call the same function AUT would have called in-process, write
the answer back. There is no second implementation of anything — that is the
point of doing the package first (§13.3), and it is what keeps "in this
process" and "over a wire" from meaning two different montages.

Every endpoint calls the `_here` half of the client rather than the
dispatching one. That is not a detail: a service that went through the
dispatcher would forward the request it was answering back to itself, over and
over, until the stack gave out. A flag saying "not this time" would work and
would be a rule somebody has to remember; two names cannot be got wrong.

Two rules the wire adds that the import never had:

* **Paths are validated against the media root** (§2.4). Files do not travel;
  paths inside a shared volume do, and a path is a request to open a file on
  somebody else's disk. Without this the service reads whatever it is asked to.
* **A shared token** (§2.6), when one is configured. The service is not meant
  to be reachable from outside at all, and this is the belt for the day
  somebody publishes its port by accident.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from montage import client
from montage import composition as comp
from montage.config import get_settings
from montage.render import compiler, probe
from montage.service import wire

log = logging.getLogger(__name__)

VERSION = "1.0.0"


def _authorised(authorization: str | None = Header(default=None)) -> None:
    """The shared token, when there is one.

    Compared in full rather than by prefix, and absent configuration means the
    door is open — the same choice AUT's own token makes, and for the same
    reason: a service that refuses to start without a secret is a service
    nobody runs locally.
    """
    expected = get_settings().service.token
    if not expected:
        return
    given = (authorization or "").removeprefix("Bearer ").strip()
    if given != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad or missing token")


def inside_root(path: str) -> str:
    """That path, if it is inside the media root; otherwise a refusal.

    The validation §2.4 asks for, and the reason it is not optional: the two
    services share a volume, so an unchecked path is "read me any file you can
    reach" with extra steps. Relative paths are resolved against the root,
    which is what makes a request portable between the two containers.
    """
    # `media_root` rather than the setting: it is the same directory, with
    # its subdirectories made, and it is the function the renderer itself
    # resolves against — so "inside the root" means the same thing on both
    # sides of the check.
    root = os.path.realpath(probe.media_root())
    candidate = os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))
    if candidate != root and not candidate.startswith(root + os.sep):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{path!r} is outside the media root; only paths under it can be used",
        )
    return candidate


class SourceRequest(BaseModel):
    source_path: str


class ComposeRequest(BaseModel):
    request: dict[str, Any]


class RenderRequest(BaseModel):
    composition: dict[str, Any]
    output_path: str
    strategy: str | None = None
    source_duration_sec: float = 0.0


class PreviewRequest(BaseModel):
    composition: dict[str, Any]
    output_path: str
    at_sec: float = 0.0
    duration_sec: float = 4.0
    scale: float = Field(default=0.5, gt=0.0, le=1.0)
    crf: int = 30


class CoverRequest(BaseModel):
    source_path: str
    output_path: str
    start_sec: float = 0.0
    title_text: str = ""
    part_text: str = ""
    thumbnail_path: str | None = None
    width: int = 1080
    height: int = 1920


def _serving() -> None:
    """While this request is being answered, the work happens here.

    Whatever this process has been told about where the montage service lives,
    a request that has arrived *at* the service is not forwarded on: that is a
    service calling itself until the stack gives out. A dependency rather than
    a start-up flag because both halves can share a process — under a test,
    and under a single-container deployment — and then "I am the service" is
    true of this request rather than of the program.
    """

def create_app() -> FastAPI:
    app = FastAPI(
        title="montage",
        version=VERSION,
        description="Scenarios, compositions and ffmpeg. AUT talks to this.",
        dependencies=[Depends(_authorised), Depends(_serving)],
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Whether this service can do its job, which means: is ffmpeg here."""
        return {
            "version": VERSION,
            "ffmpeg": client.renderer_available_here(),
            "media_root": probe.media_root(),
        }

    @app.get("/capabilities")
    def capabilities() -> dict[str, Any]:
        """What this build of ffmpeg can actually do (§7.2, Р-40)."""
        return client.capabilities_here()

    @app.post("/analyse")
    def analyse(payload: SourceRequest) -> dict[str, Any]:
        return client.analyse_here(inside_root(payload.source_path))

    @app.post("/has-audio")
    def has_audio(payload: SourceRequest) -> dict[str, bool]:
        return {"has_audio": client.has_audio_here(inside_root(payload.source_path))}

    @app.post("/compose")
    def compose(payload: ComposeRequest) -> dict[str, Any]:
        """A clip described and dressed: the expensive half of the seam.

        The source is probed here, which is the reason this is a request at
        all rather than arithmetic AUT could do itself — the file is on the
        volume this service can read.
        """
        request = wire.clip_request_from_dict(payload.request)
        inside_root(request.source_path)
        return wire.clip_plan_to_dict(client.compose_here(request))

    @app.post("/render")
    def render(payload: RenderRequest) -> dict[str, Any]:
        composition = comp.from_dict(payload.composition)
        _check_sources(composition)
        result = client.render_here(
            composition,
            inside_root(payload.output_path),
            strategy=payload.strategy,
            source_duration_sec=payload.source_duration_sec,
        )
        return wire.render_result_to_dict(result)

    @app.post("/preview")
    def preview(payload: PreviewRequest) -> dict[str, Any]:
        composition = comp.from_dict(payload.composition)
        _check_sources(composition)
        result = client.preview_here(
            composition,
            inside_root(payload.output_path),
            spec=compiler.PreviewSpec(
                at_sec=payload.at_sec, duration_sec=payload.duration_sec,
                scale=payload.scale, crf=payload.crf,
            ),
        )
        return wire.render_result_to_dict(result)

    @app.post("/cover")
    def cover(payload: CoverRequest) -> dict[str, str | None]:
        return {
            "path": client.cover_here(
                source_path=inside_root(payload.source_path),
                output_path=inside_root(payload.output_path),
                start_sec=payload.start_sec,
                title_text=payload.title_text,
                part_text=payload.part_text,
                thumbnail_path=(
                    inside_root(payload.thumbnail_path) if payload.thumbnail_path else None
                ),
                width=payload.width,
                height=payload.height,
            )
        }

    @app.get("/fragment-cache")
    def fragment_cache() -> dict[str, str]:
        """Where the service keeps its own files, so the sweeper can find them.

        AUT sweeps this today because it owns the retention schedule. Over the
        wire it is the service's own volume and the answer is a path on it —
        which is exactly the seam this endpoint exists to make honest.
        """
        directory, suffix = client.fragment_cache_here()
        return {"directory": str(directory), "suffix": suffix}

    return app


def _check_sources(composition: comp.Composition) -> None:
    """Every file a composition names has to be inside the root.

    A composition is a list of paths with instructions attached, so it is the
    same request as `/analyse` repeated — and the same refusal.
    """
    for path in composition.source_paths:
        inside_root(path)


app = create_app()
