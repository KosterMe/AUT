"""The b-roll library.

Upload a fragment, tag it, and the render pipeline starts using it: the insert
planner matches those tags against the words spoken in each clip. Nothing here
decides where anything goes — that is `app/domain/inserts.py`.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlmodel import Session

from app.api.deps import db_session
from app.api.schemas.assets import AssetRead, AssetTagsUpdate
from app.db.enums import AssetKind
from app.services import assets

router = APIRouter(prefix="/api/assets", tags=["assets"])


@router.get("", response_model=list[AssetRead])
def list_assets(
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(db_session),
):
    return assets.list_all(session, limit=limit, offset=offset)


@router.post("", response_model=AssetRead, status_code=status.HTTP_201_CREATED)
async def upload_asset(
    file: UploadFile = File(...),
    tags: str = Form(default=""),
    session: Session = Depends(db_session),
):
    """Add a fragment to the library.

    Re-uploading a file that is already there merges the tags into the existing
    asset rather than storing a second copy — two rows of identical footage
    would only compete for the same keyword.
    """
    asset = assets.save_upload(
        session, filename=file.filename or "asset", data=await file.read(), tags=tags
    )
    session.commit()
    session.refresh(asset)
    return asset


@router.patch("/{asset_id}", response_model=AssetRead)
def update_tags(
    asset_id: int, payload: AssetTagsUpdate, session: Session = Depends(db_session)
):
    asset = assets.set_tags(session, asset_id, payload.tags)
    session.commit()
    session.refresh(asset)
    return asset


@router.delete("/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_asset(asset_id: int, session: Session = Depends(db_session)):
    assets.delete(session, asset_id)
    session.commit()


@router.get("/{asset_id}/file")
def asset_file(asset_id: int, session: Session = Depends(db_session)):
    """Stream the fragment itself, so the library can be previewed."""
    asset = assets.get(session, asset_id)
    if not asset.path or not os.path.exists(asset.path):
        raise HTTPException(status_code=404, detail="the asset file is missing from disk")
    media_type = {
        AssetKind.IMAGE: "image/jpeg",
        AssetKind.AUDIO: "audio/mpeg",
    }.get(asset.kind, "video/mp4")
    return FileResponse(asset.path, media_type=media_type, filename=asset.original_name or None)
