"""TikTok accounts."""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from app.api.deps import db_session
from app.api.schemas.accounts import AccountCreate, AccountRead, AccountUpdate
from app.services import accounts

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.get("", response_model=list[AccountRead])
def list_accounts(session: Session = Depends(db_session)):
    return accounts.list_all(session)


@router.get("/{account_id}", response_model=AccountRead)
def get_account(account_id: int, session: Session = Depends(db_session)):
    return accounts.get(session, account_id)


@router.post("", response_model=AccountRead, status_code=status.HTTP_201_CREATED)
def create_account(payload: AccountCreate, session: Session = Depends(db_session)):
    """Register an account whose cookies are already on disk.

    This does not log anybody in — see /api/login.
    """
    account = accounts.register(session, payload.username, payload.display_name)
    session.commit()
    session.refresh(account)
    return account


@router.patch("/{account_id}", response_model=AccountRead)
def update_account(
    account_id: int, payload: AccountUpdate, session: Session = Depends(db_session)
):
    account = accounts.rename(session, account_id, payload.display_name)
    session.commit()
    session.refresh(account)
    return account


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(account_id: int, session: Session = Depends(db_session)):
    accounts.delete(session, account_id)
    session.commit()


@router.post("/import-from-disk", response_model=list[AccountRead])
def import_from_disk(session: Session = Depends(db_session)):
    """Pick up cookie files that have no account row yet."""
    imported = accounts.import_from_disk(session)
    session.commit()
    for account in imported:
        session.refresh(account)
    return imported


@router.post("/resync", response_model=list[AccountRead])
def resync(session: Session = Depends(db_session)):
    """Re-check every account's cookie file against its stored flag."""
    accounts.resync_session_flags(session)
    session.commit()
    return accounts.list_all(session)
