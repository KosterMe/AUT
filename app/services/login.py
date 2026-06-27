"""Getting a TikTok session into the app.

Two ways in, because they suit different deployments:

* **Import** — the user exports cookies from a browser they are already signed
  in with, and uploads the file. Works everywhere, including a headless
  server, which is why it is the primary path.
* **Local browser** — opens Chrome on this machine and waits for a manual
  sign-in. Convenient on a desktop, impossible in a container.

TikTok's login is behind a captcha; nothing here tries to solve it. The user
signs in, we keep the resulting cookies.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from typing import Any

from sqlmodel import Session

from app.adapters.tiktok import cookies as cookie_store
from app.core.clock import utc_now
from app.core.errors import NotFoundError, ValidationError
from app.db.enums import LoginStatus
from app.db.models import LoginSession
from app.db.session import session_scope
from app.services import accounts as account_service

log = logging.getLogger(__name__)


def import_cookies(session: Session, username: str, payload: str | list[dict[str, Any]]) -> LoginSession:
    """Store an exported cookie jar for `username` and register the account.

    Accepts either the JSON array produced by browser cookie-export extensions
    or a Netscape `cookies.txt`. Both are common; guessing between them is
    friendlier than making the user pick a format.
    """
    cookies = _parse_cookies(payload)
    if not any(c.get("name") == "sessionid" and c.get("value") for c in cookies):
        raise ValidationError(
            "the cookies contain no 'sessionid' — export them from a tab that is "
            "signed in to tiktok.com"
        )
    if not any(c.get("name") == "tt-target-idc" for c in cookies):
        # Not fatal: the client falls back to a default datacenter, but uploads
        # are noticeably less reliable, so say so.
        log.warning("imported cookies for '%s' have no tt-target-idc", username)

    record = LoginSession(
        id=uuid.uuid4().hex,
        username=username,
        method="import",
        status=LoginStatus.COMPLETED,
        completed_at=utc_now(),
    )
    cookie_store.save(username, cookies)
    account_service.upsert(session, username)
    session.add(record)
    session.flush()
    log.info("imported a session for '%s' (%d cookies)", username, len(cookies))
    return record


def start_local_browser(session: Session, username: str) -> LoginSession:
    """Open Chrome on this machine and capture the session in the background.

    Returns immediately; poll `get` for progress. Only usable where a desktop
    browser exists — on a server, use `import_cookies`.
    """
    record = LoginSession(
        id=uuid.uuid4().hex,
        username=username,
        method="local_browser",
        status=LoginStatus.ACTIVE,
    )
    session.add(record)
    session.flush()

    thread = threading.Thread(
        target=_run_local_browser,
        args=(record.id, username),
        name=f"tiktok-login-{record.id[:8]}",
        daemon=True,
    )
    thread.start()
    return record


def get(session: Session, session_id: str) -> LoginSession:
    record = session.get(LoginSession, session_id)
    if record is None:
        raise NotFoundError(f"login session {session_id} not found")
    return record


def cancel(session: Session, session_id: str) -> LoginSession:
    record = get(session, session_id)
    if record.status in (LoginStatus.COMPLETED, LoginStatus.FAILED):
        return record
    record.status = LoginStatus.EXPIRED
    record.completed_at = utc_now()
    session.add(record)
    session.flush()
    return record


def _run_local_browser(session_id: str, username: str) -> None:
    """Background body of the local-browser login."""
    from app.adapters.tiktok import login as browser_login

    try:
        browser_login.capture_session(username)
    except BaseException as exc:
        _finish(session_id, LoginStatus.FAILED, f"{type(exc).__name__}: {exc}")
        return

    with session_scope() as session:
        record = session.get(LoginSession, session_id)
        if record is None or record.status == LoginStatus.EXPIRED:
            return
        account_service.upsert(session, username)
        record.status = LoginStatus.COMPLETED
        record.error = None
        record.completed_at = utc_now()
        session.add(record)


def _finish(session_id: str, status: LoginStatus, error: str | None) -> None:
    with session_scope() as session:
        record = session.get(LoginSession, session_id)
        if record is None:
            return
        record.status = status
        record.error = error
        record.completed_at = utc_now()
        session.add(record)


def _parse_cookies(payload: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [_normalize(c) for c in payload if isinstance(c, dict)]

    text = payload.strip()
    if not text:
        raise ValidationError("no cookie data was provided")

    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"cookie JSON is malformed: {exc}") from exc
        if not isinstance(parsed, list):
            raise ValidationError("cookie JSON must be an array of cookie objects")
        return [_normalize(c) for c in parsed if isinstance(c, dict)]

    return _parse_netscape(text)


def _parse_netscape(text: str) -> list[dict[str, Any]]:
    """Parse a `cookies.txt` file: domain, flag, path, secure, expiry, name, value."""
    cookies: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, expiry, name, value = parts[:7]
        cookie = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "secure": secure.upper() == "TRUE",
        }
        try:
            if int(expiry) > 0:
                cookie["expiry"] = int(expiry)
        except (TypeError, ValueError):
            pass
        cookies.append(cookie)
    if not cookies:
        raise ValidationError("no cookies found — is this a Netscape cookies.txt file?")
    return cookies


def _normalize(cookie: dict[str, Any]) -> dict[str, Any]:
    """Accept the field names the various export extensions use."""
    normalized = {
        "name": cookie.get("name"),
        "value": cookie.get("value"),
        "domain": cookie.get("domain") or ".tiktok.com",
        "path": cookie.get("path") or "/",
        "secure": bool(cookie.get("secure", False)),
    }
    expiry = cookie.get("expiry") or cookie.get("expirationDate") or cookie.get("expires")
    if expiry:
        try:
            normalized["expiry"] = int(float(expiry))
        except (TypeError, ValueError):
            pass
    # Selenium rejects sameSite=None unless Secure is set; Strict always loads.
    if cookie.get("sameSite") in ("None", "no_restriction", "unspecified"):
        normalized["sameSite"] = "Strict"
    elif cookie.get("sameSite"):
        normalized["sameSite"] = cookie["sameSite"]
    return normalized
