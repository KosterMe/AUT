"""Scheduling and tracking TikTok posts."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlmodel import Session

from app.api.deps import db_session
from app.api.schemas.publications import (
    PublicationRead,
    PublicationUpdate,
    ScheduleClipRequest,
    ScheduleFileRequest,
    ScheduleJobRequest,
)
from app.db.enums import SourceKind
from app.services import publications

router = APIRouter(prefix="/api/publications", tags=["publications"])


@router.get("", response_model=list[PublicationRead])
def list_publications(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(db_session),
):
    return publications.list_all(session, status=status_filter, limit=limit, offset=offset)


@router.get("/{publication_id}", response_model=PublicationRead)
def get_publication(publication_id: int, session: Session = Depends(db_session)):
    return publications.get(session, publication_id)


@router.post("/clip/{clip_id}", response_model=PublicationRead, status_code=status.HTTP_201_CREATED)
def schedule_clip(
    clip_id: int, payload: ScheduleClipRequest, session: Session = Depends(db_session)
):
    row = publications.schedule_clip(
        session,
        clip_id=clip_id,
        username=payload.username,
        scheduled_at=payload.scheduled_at,
        options=payload.options.model_dump(),
        caption=payload.caption,
    )
    session.commit()
    session.refresh(row)
    return row


@router.post("/job/{job_id}", response_model=list[PublicationRead], status_code=status.HTTP_201_CREATED)
def schedule_job(job_id: int, payload: ScheduleJobRequest, session: Session = Depends(db_session)):
    """Queue every rendered, unscheduled clip of a job, spaced out in time."""
    rows = publications.schedule_job(
        session,
        job_id=job_id,
        username=payload.username,
        first_at=payload.first_at,
        interval_minutes=payload.interval_minutes,
        options=payload.options.model_dump(),
    )
    session.commit()
    for row in rows:
        session.refresh(row)
    return rows


@router.post("/file", response_model=PublicationRead, status_code=status.HTTP_201_CREATED)
def schedule_file(payload: ScheduleFileRequest, session: Session = Depends(db_session)):
    """Publish a file or URL that did not come from the clip pipeline."""
    row = publications.schedule_file(
        session,
        username=payload.username,
        source_ref=payload.source_ref,
        caption=payload.caption,
        scheduled_at=payload.scheduled_at,
        source_kind=SourceKind(payload.source_kind),
        options=payload.options.model_dump(),
    )
    session.commit()
    session.refresh(row)
    return row


@router.patch("/{publication_id}", response_model=PublicationRead)
def update_publication(
    publication_id: int, payload: PublicationUpdate, session: Session = Depends(db_session)
):
    row = publications.get(session, publication_id)
    if payload.scheduled_at is not None:
        row = publications.reschedule(session, publication_id, payload.scheduled_at)
    if payload.caption is not None:
        row = publications.set_caption(session, publication_id, payload.caption)
    session.commit()
    session.refresh(row)
    return row


@router.post("/{publication_id}/cancel", response_model=PublicationRead)
def cancel_publication(publication_id: int, session: Session = Depends(db_session)):
    row = publications.cancel(session, publication_id)
    session.commit()
    session.refresh(row)
    return row


@router.post("/cancel-scheduled", response_model=list[PublicationRead])
def cancel_scheduled(
    job_id: int | None = Query(default=None), session: Session = Depends(db_session)
):
    rows = publications.cancel_scheduled(session, job_id=job_id)
    session.commit()
    for row in rows:
        session.refresh(row)
    return rows


@router.post("/{publication_id}/retry", response_model=PublicationRead)
def retry_publication(publication_id: int, session: Session = Depends(db_session)):
    """Re-queue a failed publication.

    Manual on purpose: an upload can fail after TikTok accepted the video, so
    check the account before retrying or you may end up with a duplicate post.
    """
    row = publications.retry(session, publication_id)
    session.commit()
    session.refresh(row)
    return row


@router.post("/retry-untouched", response_model=list[PublicationRead])
def retry_untouched(
    job_id: int | None = Query(default=None), session: Session = Depends(db_session)
):
    """Re-queue every failure that never reached TikTok.

    Safe in bulk precisely because it skips any publication whose upload had
    started — those still need the one-at-a-time button, and a look at the
    account first. This is the way back from an expired session, which fails
    everything queued behind it one scheduled minute at a time.
    """
    rows = publications.retry_untouched(session, job_id=job_id)
    session.commit()
    for row in rows:
        session.refresh(row)
    return rows


@router.delete("/{publication_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_publication(publication_id: int, session: Session = Depends(db_session)):
    publications.delete(session, publication_id)
    session.commit()
