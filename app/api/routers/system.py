"""Health, queue visibility, and media files.

The queue endpoints exist because a queue you cannot see is a queue you cannot
operate: when nothing is publishing, the first question is always "is there a
worker running, and what is it doing?"
"""
from __future__ import annotations

import importlib.util

import os

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlmodel import Session, col, func, select

from app import __version__
from app.api.deps import db_session
from app.core.clock import isoformat_z
from app.core.config import get_settings
from app.db.enums import TaskStatus
from app.db.models import Clip, Task
from app.tasks import queue

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health(session: Session = Depends(db_session)):
    """Liveness plus the numbers that say whether work is actually moving."""
    counts = dict(
        session.exec(select(Task.status, func.count()).group_by(col(Task.status))).all()
    )
    overdue = session.exec(
        select(func.count())
        .select_from(Task)
        .where(Task.status == TaskStatus.QUEUED)
        .where(col(Task.run_at) < _now())
    ).one()

    return {
        "status": "ok",
        "version": __version__,
        "environment": get_settings().environment,
        "tasks": {str(k): v for k, v in counts.items()},
        # Steadily rising means no worker is serving these kinds.
        "tasks_due_now": overdue,
        # Whether the local-browser login can work on this host at all, so the
        # UI can stop offering a button that only ever returns 503 here.
        "browser_login": browser_login_available(),
    }


def browser_login_available() -> bool:
    """Whether `POST /api/login/browser` has any chance of working here.

    It drives a real Chrome through undetected-chromedriver and waits for a
    person to sign in past TikTok's captcha, so it needs both the dependency
    and a desktop in front of the machine running the API. In the container
    image neither holds — the dependency is deliberately left out of
    requirements.txt, and a browser opened inside a container is one nobody
    can reach — which is why importing an exported cookie file is the primary
    path and this is a desktop convenience.

    The module check stands in for the whole question: it is present exactly
    in the desktop install this flow was built for.
    """
    return importlib.util.find_spec("undetected_chromedriver") is not None


@router.get("/tasks")
def list_tasks(
    status_filter: str | None = Query(default=None, alias="status"),
    kind: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(db_session),
):
    query = select(Task).order_by(col(Task.created_at).desc()).limit(limit)
    if status_filter:
        query = query.where(Task.status == status_filter)
    if kind:
        query = query.where(Task.kind == kind)
    return [
        {
            "id": task.id,
            "kind": task.kind,
            "status": task.status,
            "stage": task.stage,
            "progress": task.progress,
            "attempts": task.attempts,
            "max_attempts": task.max_attempts,
            # Serialized like every other timestamp the API returns. The
            # column is naive UTC, so handing it over unmarked makes a
            # browser read it as local time — three hours out here.
            "run_at": isoformat_z(task.run_at),
            "error": task.error,
            "payload": queue.payload_of(task),
        }
        for task in session.exec(query).all()
    ]


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int, session: Session = Depends(db_session)):
    task = queue.request_cancel(session, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    session.commit()
    return {"id": task.id, "status": task.status, "cancel_requested": task.cancel_requested}


@router.get("/clips/{clip_id}/video")
def clip_video(clip_id: int, session: Session = Depends(db_session)):
    """Stream a rendered clip so the UI can preview it before scheduling."""
    return _serve(session, clip_id, cover=False)


@router.get("/clips/{clip_id}/cover")
def clip_cover(clip_id: int, session: Session = Depends(db_session)):
    return _serve(session, clip_id, cover=True)


def _serve(session: Session, clip_id: int, *, cover: bool) -> FileResponse:
    clip = session.get(Clip, clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="clip not found")
    path = clip.cover_path if cover else clip.video_path
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="file has not been rendered yet")
    return FileResponse(path, media_type="image/jpeg" if cover else "video/mp4")


def _now():
    from app.core.clock import utc_now

    return utc_now()
