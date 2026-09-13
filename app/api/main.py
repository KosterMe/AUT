"""FastAPI application.

An entrypoint, so this is one of the few places allowed to read `.env` and
configure logging. Routers are mounted here and nowhere else.

The API does not run background work. Workers are separate processes
(`python -m app.workers`), which is what makes it possible to scale rendering
without scaling the web tier — and what stops a long ffmpeg run from competing
with request handling for the same interpreter.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import errors
from app.api.deps import require_auth
from app.api.routers import (
    accounts,
    assets,
    clip_jobs,
    clips,
    login,
    publications,
    scenarios,
    styles,
    system,
)
from app.core.config import get_settings, load_dotenv_for_entrypoint
from app.core.logging import configure_logging

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.paths.ensure()
    log.info("API %s starting in %s mode", __version__, settings.environment)
    if not settings.api.auth_token:
        log.warning(
            "APP_AUTH_TOKEN is not set — the API is unauthenticated. That is fine "
            "bound to localhost, but set it before exposing this port."
        )
    _seed_scenarios()
    _seed_cleanup()
    yield
    log.info("API stopped")


def _seed_scenarios() -> None:
    """Make sure the four built-in montages are in the database.

    They are code rather than data — `montage/scenario/builtin.py` is the
    source — so their rows are refreshed at every start. An operator's own
    scenarios are never touched.
    """
    from app.db.session import session_scope
    from app.services import scenarios

    try:
        with session_scope() as session:
            scenarios.seed(session)
    except Exception:  # pragma: no cover - a job can still name a built-in
        log.exception("could not seed the built-in scenarios")


def _seed_cleanup() -> None:
    """Put the recurring media sweep on the queue if it is not already there.

    The API runs no background work itself, and this does not change that — it
    queues one task for a worker. It is here because nothing else was: the
    cleanup handler was registered, a worker was serving its kind, and no code
    path had ever created a single cleanup task, so retention never ran and
    `data/media` grew without a ceiling.
    """
    from app.db.session import session_scope
    from app.tasks.handlers import cleanup

    try:
        with session_scope() as session:
            cleanup.ensure_scheduled(session)
    except Exception:  # pragma: no cover - a broken sweep must not stop the API
        log.exception("could not schedule the periodic media cleanup")


def create_app() -> FastAPI:
    load_dotenv_for_entrypoint()
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)

    app = FastAPI(
        title="TikTok Auto Poster",
        version=__version__,
        description=(
            "Cut long videos into vertical clips and schedule them to TikTok.\n\n"
            "Flow: register a source (`POST /api/jobs`) → it is downloaded, "
            "transcribed and cut into clips → schedule clips "
            "(`POST /api/publications/job/{id}`) → workers publish them at the "
            "requested times."
        ),
        lifespan=lifespan,
        dependencies=[Depends(require_auth)],
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.api.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )

    errors.install(app)

    app.include_router(system.router)
    app.include_router(accounts.router)
    app.include_router(assets.router)
    app.include_router(clip_jobs.router)
    app.include_router(clips.router)
    app.include_router(styles.router)
    app.include_router(scenarios.router)
    app.include_router(publications.router)
    app.include_router(login.router)
    return app


app = create_app()
