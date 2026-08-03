"""Clip jobs and the clips they produce."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlmodel import Session

from app.api.deps import db_session
from app.api.schemas.clips import (
    ClipJobCreate,
    ClipJobDetail,
    ClipJobRead,
    ClipJobStart,
    ClipJobTagsUpdate,
    ClipJobTitleUpdate,
    ClipRead,
)
from app.services import clip_jobs, clips

router = APIRouter(prefix="/api/jobs", tags=["clip jobs"])


@router.get("", response_model=list[ClipJobRead])
def list_jobs(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(db_session),
):
    return clip_jobs.list_all(session, limit=limit, offset=offset)


@router.post("", response_model=ClipJobRead, status_code=status.HTTP_201_CREATED)
def create_job(payload: ClipJobCreate, session: Session = Depends(db_session)):
    """Register a source video and, by default, start cutting it right away."""
    job = clip_jobs.create(
        session,
        source_ref=payload.source_ref,
        source_platform=payload.source_platform,
        title=payload.title,
        custom_title=payload.custom_title,
        caption_tags=payload.caption_tags,
        profile=payload.profile,
        start_immediately=payload.start_immediately,
        min_clip_seconds=payload.min_clip_seconds,
        max_clip_seconds=payload.max_clip_seconds,
        gap_seconds=payload.gap_seconds,
        max_clips=payload.max_clips,
        render=payload.render.model_dump(),
    )
    session.commit()
    session.refresh(job)
    return job


@router.get("/{job_id}", response_model=ClipJobDetail)
def get_job(job_id: int, session: Session = Depends(db_session)):
    """A job together with its clips — one request for the whole job page."""
    job = clip_jobs.get(session, job_id)
    detail = ClipJobDetail.model_validate(job)
    detail.clips = _read_clips(session, job_id)
    return detail


@router.post("/{job_id}/start", response_model=ClipJobRead, status_code=status.HTTP_202_ACCEPTED)
def start_job(
    job_id: int, payload: ClipJobStart = ClipJobStart(), session: Session = Depends(db_session)
):
    """Queue (or re-queue) the download → transcribe → plan pass."""
    clip_jobs.start(
        session,
        job_id,
        min_clip_seconds=payload.min_clip_seconds,
        max_clip_seconds=payload.max_clip_seconds,
        gap_seconds=payload.gap_seconds,
        max_clips=payload.max_clips,
        profile=payload.profile,
        render=payload.render.model_dump(),
    )
    session.commit()
    return clip_jobs.get(session, job_id)


@router.post("/{job_id}/cancel", response_model=ClipJobRead)
def cancel_job(job_id: int, session: Session = Depends(db_session)):
    job = clip_jobs.cancel(session, job_id)
    session.commit()
    session.refresh(job)
    return job


@router.patch("/{job_id}/title", response_model=ClipJobRead)
def update_title(
    job_id: int, payload: ClipJobTitleUpdate, session: Session = Depends(db_session)
):
    job = clip_jobs.set_custom_title(session, job_id, payload.custom_title)
    session.commit()
    session.refresh(job)
    return job


@router.patch("/{job_id}/caption-tags", response_model=ClipJobRead)
def update_caption_tags(
    job_id: int, payload: ClipJobTagsUpdate, session: Session = Depends(db_session)
):
    job = clip_jobs.set_caption_tags(session, job_id, payload.caption_tags)
    session.commit()
    session.refresh(job)
    return job


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(job_id: int, session: Session = Depends(db_session)):
    clip_jobs.delete(session, job_id)
    session.commit()


@router.get("/{job_id}/clips", response_model=list[ClipRead])
def list_job_clips(
    job_id: int,
    unpublished_only: bool = Query(default=False),
    session: Session = Depends(db_session),
):
    clip_jobs.get(session, job_id)  # 404 for an unknown job
    if unpublished_only:
        return [ClipRead.model_validate(clip) for clip in clips.unpublished(session, job_id)]
    return _read_clips(session, job_id)


def _read_clips(session: Session, job_id: int) -> list[ClipRead]:
    """Clips annotated with the publication that owns each of them, if any."""
    owners = clips.publications_by_clip(session, job_id)
    result = []
    for clip in clips.list_for_job(session, job_id):
        read = ClipRead.model_validate(clip)
        publication = owners.get(int(clip.id))
        if publication is not None:
            read.publication_id = publication.id
            read.publication_status = publication.status
        result.append(read)
    return result
