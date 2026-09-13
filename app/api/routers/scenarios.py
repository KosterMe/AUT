"""Scenarios: what a job can be rendered with, listed and read.

Read-only for now, and deliberately: `POST /api/jobs` takes a `scenario_id`,
and a field whose values cannot be discovered is a field nobody can use. The
editing half — saving one, previewing it, taking it apart on a mock duration
— belongs to the editor and arrives with it (§12, этап 4).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.api.deps import db_session
from app.api.schemas.scenarios import ScenarioRead
from app.core.jsonutil import loads_dict
from app.db.models import Scenario
from app.services import scenarios

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])


@router.get("", response_model=list[ScenarioRead])
def list_scenarios(session: Session = Depends(db_session)):
    """Built-ins first, then the operator's own."""
    return [_read(row) for row in scenarios.list_all(session)]


@router.get("/{scenario_id}", response_model=ScenarioRead)
def get_scenario(scenario_id: int, session: Session = Depends(db_session)):
    return _read(scenarios.get(session, scenario_id))


def _read(row: Scenario) -> ScenarioRead:
    """The row with its scenario as an object rather than a string.

    Stored as text because that is what a database column is; sent as JSON
    because a client that has to parse a string out of a response has been
    handed the storage format instead of the value.
    """
    return ScenarioRead(
        id=row.id,
        name=row.name,
        description=row.description,
        builtin=row.builtin,
        data=loads_dict(row.data_json),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
