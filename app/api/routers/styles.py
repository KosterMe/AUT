"""Named looks: list them, save one, ask what the defaults are.

`GET /api/styles/defaults` is the endpoint that makes the rest optional. It
answers with the complete resolved style for a profile, so a form can show
every value that will actually be used without anybody having filled anything
in — and then save back only what they changed.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query, status
from sqlmodel import Session

from app.api.deps import db_session
from app.api.schemas.styles import StyleCreate, StyleDefaults, StyleRead, StyleUpdate
from app.db.models import StylePreset
from app.domain import profiles
from montage import style as style_module
from app.services import styles

router = APIRouter(prefix="/api/styles", tags=["styles"])


@router.get("", response_model=list[StyleRead])
def list_styles(session: Session = Depends(db_session)):
    return [_read(preset) for preset in styles.list_all(session)]


@router.get("/defaults", response_model=StyleDefaults)
def style_defaults(profile: str | None = Query(default=None)):
    """Every value a profile renders with when no preset is attached."""
    chosen = profiles.get(profile)
    return StyleDefaults(
        profile=chosen.name,
        style=styles.defaults_for(chosen.name).to_dict(),
        profiles=list(profiles.NAMES),
    )


@router.get("/{style_id}", response_model=StyleRead)
def get_style(style_id: int, session: Session = Depends(db_session)):
    return _read(styles.get(session, style_id))


@router.post("", response_model=StyleRead, status_code=status.HTTP_201_CREATED)
def create_style(payload: StyleCreate, session: Session = Depends(db_session)):
    preset = styles.create(
        session,
        name=payload.name,
        description=payload.description,
        profile=payload.profile,
        data=payload.data,
    )
    session.commit()
    session.refresh(preset)
    return _read(preset)


@router.patch("/{style_id}", response_model=StyleRead)
def update_style(style_id: int, payload: StyleUpdate, session: Session = Depends(db_session)):
    preset = styles.update(
        session,
        style_id,
        name=payload.name,
        description=payload.description,
        profile=payload.profile,
        data=payload.data,
    )
    session.commit()
    session.refresh(preset)
    return _read(preset)


@router.delete("/{style_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_style(style_id: int, session: Session = Depends(db_session)):
    styles.delete(session, style_id)
    session.commit()


def _read(preset: StylePreset) -> StyleRead:
    """A preset with both its overrides and what they resolve to.

    The UI needs the pair: the overrides say which controls this preset has an
    opinion about, and the resolved style says what every other control is
    going to do anyway.
    """
    try:
        data = json.loads(preset.data_json or "{}")
    except json.JSONDecodeError:
        data = {}
    resolved = style_module.resolve(
        profile=profiles.style_overrides(profiles.get(preset.profile)),
        preset=data if isinstance(data, dict) else {},
    )
    return StyleRead(
        id=int(preset.id),
        name=preset.name,
        description=preset.description,
        profile=preset.profile,
        data=data if isinstance(data, dict) else {},
        resolved=resolved.to_dict(),
        created_at=preset.created_at,
        updated_at=preset.updated_at,
    )
