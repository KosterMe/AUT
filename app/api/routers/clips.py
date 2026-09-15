"""One clip at a time: what it is made of, render it again, look at it first.

The preview is the endpoint this whole thing exists for. Rendering a clip is
twenty seconds and a file nobody keeps, so before it a change to the look was
a guess; four seconds at half size comes back while you are still looking at
the control you moved. It renders synchronously — FastAPI runs a sync endpoint
in a worker thread, and the alternative, a task and a poll, costs more than the
render does.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, status
from fastapi.responses import FileResponse
from sqlmodel import Session

from app.api.deps import db_session
from app.api.schemas.clips import ClipPreviewRequest, ClipRead, ClipRenderRequest
from app.services import clip_jobs, clips, rendering, scenarios, styles

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/clips", tags=["clips"])


@router.get("/{clip_id}/composition")
def clip_composition(clip_id: int, session: Session = Depends(db_session)):
    """What this clip was last rendered from — segments, overlays, style.

    Empty until it has been rendered once. Reading it is how the UI shows
    where the cuts landed and which fragments went on screen without opening
    the video.
    """
    return {"clip_id": clip_id, "composition": clips.composition_of(session, clip_id)}


@router.post("/{clip_id}/render", response_model=ClipRead, status_code=status.HTTP_202_ACCEPTED)
def rerender_clip(
    clip_id: int,
    payload: ClipRenderRequest = ClipRenderRequest(),
    session: Session = Depends(db_session),
):
    """Queue this one clip again, keeping its boundaries.

    With no body it repeats the render it already had, which is what to do
    after adding fragments to the b-roll library. With a style it re-renders
    into the new look — and only this clip, so a preset can be tried on one
    before it is put on a job.
    """
    clip = clips.get(session, clip_id)
    job = clip_jobs.get(session, clip.job_id)
    style = None
    if payload.style_id is not None or payload.style is not None:
        style = styles.resolved_for(
            session,
            profile_name=job.profile,
            style_id=payload.style_id if payload.style_id is not None else job.style_id,
            overrides=payload.style,
        ).to_dict()

    updated = clips.rerender(session, clip_id, style=style)
    session.commit()
    session.refresh(updated)
    return updated


@router.post("/{clip_id}/preview")
def preview_clip(
    clip_id: int,
    payload: ClipPreviewRequest = ClipPreviewRequest(),
    session: Session = Depends(db_session),
):
    """Render a few seconds of this clip in a given style and return the file.

    Composed by exactly the same code the real render uses, so what comes back
    is the clip, not an approximation of it — only shorter and smaller. The one
    difference is that it will not start a transcription: a preview waits on
    nothing, and a window whose words are not on disk yet gets the job's
    transcript instead.
    """
    clip = clips.get(session, clip_id)
    job = clip_jobs.get(session, clip.job_id)

    style = styles.resolved_for(
        session,
        profile_name=job.profile,
        style_id=payload.style_id if payload.style_id is not None else job.style_id,
        overrides=payload.style,
    )
    output_path = rendering.preview(
        session, clip, job,
        style=style,
        scenario=scenarios.for_job(session, job, style),
        at_sec=payload.at_sec,
        duration_sec=payload.duration_sec,
        scale=payload.scale,
    )
    return FileResponse(
        output_path,
        media_type="video/mp4",
        # No caching: the next preview is the same URL with different pixels,
        # which is the whole point of the loop.
        headers={"Cache-Control": "no-store"},
    )
