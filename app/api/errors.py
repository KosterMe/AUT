"""Domain errors -> HTTP status codes, in one place.

Routers raise nothing themselves: they call a service, and if it refuses, the
refusal is already typed. That keeps "a clip must be rendered before it can be
scheduled" as a rule of the domain rather than a 409 hard-coded in a handler.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    SessionExpiredError,
    ValidationError,
)

log = logging.getLogger(__name__)

_STATUS = {
    NotFoundError: 404,
    ConflictError: 409,
    ValidationError: 422,
    SessionExpiredError: 409,
}


def install(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _handle_domain_error(_request: Request, exc: DomainError) -> JSONResponse:
        status_code = next(
            (code for kind, code in _STATUS.items() if isinstance(exc, kind)), 400
        )
        return JSONResponse(status_code=status_code, content={"detail": str(exc)})

    @app.exception_handler(FileNotFoundError)
    async def _handle_missing_file(_request: Request, exc: FileNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})
