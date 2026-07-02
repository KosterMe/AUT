"""Getting a TikTok session into the app.

Importing exported cookies is the path that works everywhere, including a
headless server. The local-browser flow is a desktop convenience and returns
503 where no browser exists.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlmodel import Session

from app.api.deps import db_session
from app.api.schemas.login import (
    CookieImportRequest,
    LocalBrowserLoginRequest,
    LoginSessionRead,
)
from app.services import login

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/login", tags=["login"])


@router.post("/import", response_model=LoginSessionRead, status_code=status.HTTP_201_CREATED)
def import_cookies(payload: CookieImportRequest, session: Session = Depends(db_session)):
    """Store cookies exported from a signed-in browser.

    Accepts the JSON array a cookie-export extension produces or the contents
    of a Netscape cookies.txt.
    """
    record = login.import_cookies(session, payload.username, payload.cookies)
    session.commit()
    session.refresh(record)
    return record


@router.post("/import-file", response_model=LoginSessionRead, status_code=status.HTTP_201_CREATED)
async def import_cookie_file(
    username: str = Form(...),
    file: UploadFile = File(...),
    session: Session = Depends(db_session),
):
    """Same as /import, for a file picked in the browser."""
    raw = (await file.read()).decode("utf-8", errors="replace")
    record = login.import_cookies(session, username, raw)
    session.commit()
    session.refresh(record)
    return record


@router.post("/browser", response_model=LoginSessionRead, status_code=status.HTTP_201_CREATED)
def start_local_browser(
    payload: LocalBrowserLoginRequest, session: Session = Depends(db_session)
):
    """Open Chrome on the machine running the API and wait for a manual sign-in.

    Returns immediately; poll GET /api/login/{id} for the outcome.
    """
    try:
        record = login.start_local_browser(session, payload.username)
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "browser login is unavailable on this host "
                f"({exc}). Export cookies from your own browser and use /api/login/import."
            ),
        )
    session.commit()
    session.refresh(record)
    return record


@router.get("/{session_id}", response_model=LoginSessionRead)
def get_login_session(session_id: str, session: Session = Depends(db_session)):
    return login.get(session, session_id)


@router.delete("/{session_id}", response_model=LoginSessionRead)
def cancel_login(session_id: str, session: Session = Depends(db_session)):
    record = login.cancel(session, session_id)
    session.commit()
    session.refresh(record)
    return record
