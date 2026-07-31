"""Handler: upload one publication to TikTok.

The only handler with an irreversible side effect, which shapes everything
here:

* the task is marked irreversible immediately *before* the upload starts, so a
  worker that dies mid-publish leaves a failed task for a human rather than a
  retry that could post the video twice;
* a rejected session fails permanently and flags the account, because retrying
  an expired login can only fail the same way.
"""
from __future__ import annotations

import logging
import os

from app.adapters.tiktok import client as tiktok
from app.core.errors import PermanentError, SessionExpiredError
from app.core.jsonutil import loads_dict
from app.db.enums import PublicationStatus, SourceKind, TaskKind
from app.db.models import Publication
from app.services import accounts, publications
from app.tasks.context import TaskContext
from app.tasks.registry import register_handler

log = logging.getLogger(__name__)


def _on_failure(ctx: TaskContext, message: str, final: bool) -> None:
    """Make sure a publication never sits in 'publishing' with nothing running.

    The handler marks its own failures, but a worker killed mid-upload never
    gets to. The queue's reclaim sweep calls this instead, which is the only
    way that row leaves a state the UI will not let anyone act on.
    """
    if not final:
        return
    publication_id = ctx.get("publication_id")
    if publication_id is None:
        return
    with ctx.db() as session:
        publication = session.get(Publication, int(publication_id))
        if publication is None:
            return
        # A safety net, not an overwrite: when the handler itself recorded the
        # outcome it knows more than this does — in particular whether the
        # upload had already started.
        if PublicationStatus(publication.status).is_terminal:
            return
        publications.mark_failed(session, int(publication_id), message)


@register_handler(TaskKind.PUBLISH, on_failure=_on_failure)
def handle_publish(ctx: TaskContext) -> dict:
    publication_id = ctx.require_int("publication_id")

    with ctx.db() as session:
        publication = publications.get(session, publication_id)
        if publication.status == PublicationStatus.PUBLISHED:
            # Already done — most likely a retry of a task whose result never
            # got written. Publishing again would duplicate the post.
            return {"publication_id": publication_id, "skipped": "already published"}
        if publication.status == PublicationStatus.CANCELLED:
            return {"publication_id": publication_id, "skipped": "cancelled"}

        account = accounts.get(session, publication.account_id)
        if not account.has_valid_session:
            publications.mark_failed(session, publication_id, "account needs to be logged in again")
            raise PermanentError(f"account '{account.username}' has no valid session")

        plan = _Plan(
            username=account.username,
            account_id=account.id,
            source_kind=publication.source_kind,
            source_ref=publication.source_ref,
            caption=publication.caption,
            options=loads_dict(publication.options_json),
        )

    ctx.progress("resolving_source", 0.05)
    video_path = _resolve_video(plan)

    with ctx.db() as session:
        publications.mark_publishing(session, publication_id)

    # Past this line the upload may become visible on TikTok.
    ctx.mark_irreversible()

    try:
        video_id = tiktok.upload_video(
            plan.username,
            video_path,
            plan.caption,
            allow_comment=int(plan.options.get("allow_comment", 1)),
            allow_duet=int(plan.options.get("allow_duet", 0)),
            allow_stitch=int(plan.options.get("allow_stitch", 0)),
            visibility_type=int(plan.options.get("visibility_type", 0)),
            brand_organic_type=int(plan.options.get("brand_organic_type", 0)),
            branded_content_type=int(plan.options.get("branded_content_type", 0)),
            ai_label=int(plan.options.get("ai_label", 0)),
            proxy=str(plan.options.get("proxy", "")),
            on_progress=ctx.progress,
        )
    except SessionExpiredError as exc:
        with ctx.db() as session:
            accounts.invalidate_session(session, plan.account_id)
            publications.mark_failed(
                session, publication_id, f"session expired: {exc} — log the account in again"
            )
        raise
    except Exception as exc:
        # The upload was already in flight, so TikTok may have accepted the
        # video before the connection broke. Say so on the publication itself:
        # the retry button is one click away, and a bare "connection aborted"
        # reads like nothing happened.
        with ctx.db() as session:
            publications.mark_failed(
                session,
                publication_id,
                f"{type(exc).__name__}: {exc} — the upload had already started, "
                f"so check the account for '{plan.username}' before retrying",
            )
        raise

    with ctx.db() as session:
        publications.mark_published(session, publication_id, video_id=video_id)

    log.info("published publication %s as TikTok video %s", publication_id, video_id)
    return {"publication_id": publication_id, "video_id": video_id}


class _Plan:
    """Everything the upload needs, copied out of the database session.

    An upload takes minutes; holding a session open across it would pin a
    connection (and on SQLite, block writers) for the whole time.
    """

    __slots__ = ("username", "account_id", "source_kind", "source_ref", "caption", "options")

    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


def _resolve_video(plan: _Plan) -> str:
    if plan.source_kind == SourceKind.YOUTUBE:
        from app.adapters.youtube import downloader

        path, _ = downloader.download_source(plan.source_ref, job_id=0)
        return path

    if not plan.source_ref or not os.path.exists(plan.source_ref):
        raise PermanentError(f"video file is missing: {plan.source_ref}")
    return plan.source_ref
