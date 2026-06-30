"""Request dependencies: the database session and the auth seam.

Authentication is a skeleton on purpose. With `APP_AUTH_TOKEN` unset the API
is open, which is right for a single user on localhost and wrong for anything
reachable from outside. Setting it turns on bearer-token checking everywhere
without touching a single router — and when real multi-user support arrives,
`current_owner` is the one function that has to start returning a user.
"""
from __future__ import annotations

import hmac
from collections.abc import Iterator

from fastapi import Depends, Header, HTTPException, status
from sqlmodel import Session

from app.core.config import get_settings
from app.db.session import get_session


def db_session() -> Iterator[Session]:
    yield from get_session()


DbSession = Depends(db_session)


def require_auth(authorization: str | None = Header(default=None)) -> None:
    """Check the bearer token when one is configured; otherwise allow."""
    expected = get_settings().api.auth_token
    if not expected:
        return

    scheme, _, token = (authorization or "").partition(" ")
    # Constant-time compare so a wrong token cannot be found byte by byte.
    if scheme.lower() != "bearer" or not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="a valid bearer token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )


def current_owner() -> int | None:
    """Who the request belongs to.

    Always None today — everything is owned by the single operator. Every table
    already carries a nullable `owner_id`, so turning this into a real lookup
    is a change here plus a filter in the services, not a schema migration.
    """
    return None
