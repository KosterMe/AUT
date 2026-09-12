"""TikTok accounts.

An account is a username paired with a cookie jar on disk. The database row is
bookkeeping; the cookie file is the thing that actually matters, which is why
`import_from_disk` exists — cookies can arrive without the database knowing.
"""
from __future__ import annotations

import logging

from sqlmodel import Session, col, select

from app.core.clock import utc_now
from app.core.errors import ConflictError, NotFoundError
from app.db.models import Account, Publication
from app.adapters.tiktok import cookies as cookie_store

log = logging.getLogger(__name__)


def list_all(session: Session) -> list[Account]:
    rows = list(session.exec(select(Account).order_by(col(Account.username))).all())
    for account in rows:
        _drop_flag_if_the_file_is_gone(session, account)
    return rows


def get(session: Session, account_id: int) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise NotFoundError(f"account {account_id} not found")
    _drop_flag_if_the_file_is_gone(session, account)
    return account


def get_by_username(session: Session, username: str) -> Account:
    account = session.exec(select(Account).where(Account.username == username)).first()
    if account is None:
        raise NotFoundError(f"account '{username}' not found")
    _drop_flag_if_the_file_is_gone(session, account)
    return account


def get_ready(session: Session, username: str) -> Account:
    """An account that can actually publish right now."""
    account = get_by_username(session, username)
    if not account.has_valid_session:
        raise ConflictError(
            f"account '{username}' needs to be logged in again"
            if cookie_store.exists(username)
            else f"account '{username}' has no cookie file at "
            f"{cookie_store.cookie_path(username)} — import a session for it"
        )
    return account


def _drop_flag_if_the_file_is_gone(session: Session, account: Account) -> None:
    """Clear `has_valid_session` when the cookie file can no longer back it.

    The flag is a cache, and it went stale in the one direction that hurts:
    a real account showed as signed in for weeks after its cookie file
    disappeared, and the first thing to notice was a scheduled post failing at
    the minute it was due — followed by eleven more queued behind it. Reading
    an account is the moment to find out, because that is when the UI, and
    `get_ready`, are about to state it as fact.

    Only ever downgrades. A file that still holds a sessionid says nothing
    about whether TikTok honours it, so raising the flag from here would undo
    what `invalidate_session` learned the hard way; `resync_session_flags` is
    the deliberate, user-triggered way to do that.
    """
    if not account.has_valid_session or cookie_store.has_valid_session(account.username):
        return
    account.has_valid_session = False
    account.updated_at = utc_now()
    session.add(account)
    log.warning(
        "account '%s' was marked as signed in, but %s holds no session",
        account.username,
        cookie_store.cookie_path(account.username),
    )


def register(session: Session, username: str, display_name: str | None = None) -> Account:
    """Create the row for a username whose cookie file already exists."""
    if not cookie_store.exists(username):
        raise ConflictError(
            f"no cookie file for '{username}' at {cookie_store.cookie_path(username)}. "
            "Log in or import cookies first."
        )
    if session.exec(select(Account).where(Account.username == username)).first():
        raise ConflictError(f"account '{username}' already exists")

    account = Account(
        username=username,
        display_name=display_name,
        cookie_path=str(cookie_store.cookie_path(username)),
        has_valid_session=cookie_store.has_valid_session(username),
    )
    session.add(account)
    session.flush()
    return account


def upsert(session: Session, username: str) -> Account:
    """Create or refresh the row after a successful login/import."""
    account = session.exec(select(Account).where(Account.username == username)).first()
    if account is None:
        account = Account(username=username, cookie_path=str(cookie_store.cookie_path(username)))
        session.add(account)
    account.cookie_path = str(cookie_store.cookie_path(username))
    account.has_valid_session = True
    account.last_used_at = utc_now()
    account.updated_at = utc_now()
    session.add(account)
    session.flush()
    return account


def rename(session: Session, account_id: int, display_name: str | None) -> Account:
    account = get(session, account_id)
    account.display_name = display_name
    account.updated_at = utc_now()
    session.add(account)
    session.flush()
    return account


def delete(session: Session, account_id: int) -> None:
    """Remove the account and its cookies.

    Refused while publications still point at it. They are the record of what
    was posted and to which account, and the foreign key would stop the delete
    anyway — as a bare IntegrityError surfacing as HTTP 500, which is how this
    was found.

    The row goes before the file: if deleting the file then fails, the user can
    re-import it. The reverse order leaves a row pointing at nothing.
    """
    account = get(session, account_id)
    username = account.username
    published = session.exec(
        select(col(Publication.id)).where(Publication.account_id == account_id)
    ).all()
    if published:
        raise ConflictError(
            f"account '{username}' still has {len(published)} publication(s) on record. "
            "Delete or reassign them first — they are the history of what this "
            "account posted."
        )
    session.delete(account)
    session.flush()
    cookie_store.delete(username)
    log.info("deleted account '%s'", username)


def invalidate_session(session: Session, account_id: int) -> None:
    """Mark an account as needing a new login.

    Called when TikTok rejects a stored session, so the UI can say so instead
    of every scheduled publication failing one at a time.
    """
    account = session.get(Account, account_id)
    if account is None:
        return
    account.has_valid_session = False
    account.updated_at = utc_now()
    session.add(account)
    log.warning("session for '%s' was rejected by TikTok; re-login required", account.username)


def import_from_disk(session: Session) -> list[Account]:
    """Register every cookie file that has no account row yet."""
    known = {a.username for a in session.exec(select(Account)).all()}
    imported: list[Account] = []
    for username in cookie_store.list_usernames():
        if username in known:
            continue
        account = Account(
            username=username,
            cookie_path=str(cookie_store.cookie_path(username)),
            has_valid_session=cookie_store.has_valid_session(username),
        )
        session.add(account)
        imported.append(account)
    if imported:
        session.flush()
        log.info("imported %d account(s) from disk", len(imported))
    return imported


def resync_session_flags(session: Session) -> int:
    """Re-check every account's cookie file against the database flag."""
    changed = 0
    for account in list_all(session):
        actual = cookie_store.has_valid_session(account.username)
        if actual != account.has_valid_session:
            account.has_valid_session = actual
            account.updated_at = utc_now()
            session.add(account)
            changed += 1
    return changed
