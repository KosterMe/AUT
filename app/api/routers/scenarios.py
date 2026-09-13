"""Scenarios: list them, save one, and see what one does before rendering it.

`GET /{id}/inspect` is the endpoint the editor is built on. It compiles the
scenario against a clip of a given length that does not exist and reports
where everything landed — which is the only honest way to answer "what does
this do on a short one", and the question §8.2 calls the one an operator who
has not asked it has not checked their scenario.

`POST /{id}/preview` is the other half: the same scenario on a real clip,
540×960 and fast, rendered by the same code as the final. Between the two
there is no third answer — nothing here draws its own picture of what a
scenario means.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import FileResponse
from sqlmodel import Session

from app.api.deps import db_session
from app.api.schemas.scenarios import (
    InspectBlock,
    InspectGhost,
    InspectRect,
    InspectRule,
    InspectWarning,
    ScenarioCreate,
    ScenarioInspect,
    ScenarioPreviewRequest,
    ScenarioRead,
    ScenarioUpdate,
)
from app.core.errors import ValidationError
from app.core.jsonutil import loads_dict
from app.db.models import Scenario
from app.services import assets, clip_jobs, clips, rendering, scenarios, styles
from montage.scenario import inspect as inspector, mock, store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])


@router.get("", response_model=list[ScenarioRead])
def list_scenarios(session: Session = Depends(db_session)):
    """Built-ins first, then the operator's own."""
    return [_read(row) for row in scenarios.list_all(session)]


@router.get("/{scenario_id}", response_model=ScenarioRead)
def get_scenario(scenario_id: int, session: Session = Depends(db_session)):
    return _read(scenarios.get(session, scenario_id))


@router.post("", response_model=ScenarioRead, status_code=status.HTTP_201_CREATED)
def create_scenario(payload: ScenarioCreate, session: Session = Depends(db_session)):
    row = scenarios.create(
        session, name=payload.name, description=payload.description, data=payload.data,
    )
    session.commit()
    session.refresh(row)
    return _read(row)


@router.put("/{scenario_id}", response_model=ScenarioRead)
def update_scenario(
    scenario_id: int, payload: ScenarioUpdate, session: Session = Depends(db_session)
):
    """Save an edit — or, for a built-in, save a copy of it.

    The copy is the answer rather than a refusal: the operator asked to change
    something, and "no" would leave them without what they asked for. The
    response carries the id that was actually written, which may not be the
    one in the URL.
    """
    row = scenarios.update(
        session, scenario_id,
        data=payload.data,
        name=payload.name or "",
        description=payload.description,
    )
    session.commit()
    session.refresh(row)
    return _read(row)


@router.delete("/{scenario_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_scenario(scenario_id: int, session: Session = Depends(db_session)):
    scenarios.delete(session, scenario_id)
    session.commit()


@router.get("/{scenario_id}/inspect", response_model=ScenarioInspect)
def inspect_scenario(
    scenario_id: int,
    duration_sec: float | None = Query(default=None, ge=1.0, le=1800.0),
    session: Session = Depends(db_session),
):
    """This scenario on a clip of that length, taken apart.

    The library is the real one: a b-roll rule that can never fire because
    nothing is tagged should look exactly like that, and inventing fragments
    to make the picture busier would hide the one thing worth seeing.
    """
    row = scenarios.get(session, scenario_id)
    scenario = scenarios.stored(session, scenario_id)
    report = inspector.inspect(scenario, mock.facts(
        scenario, duration_sec, assets=tuple(assets.options_for_planner(session)),
    ))
    return _inspect(row, report)


@router.post("/{scenario_id}/preview")
def preview_scenario(
    scenario_id: int,
    payload: ScenarioPreviewRequest,
    session: Session = Depends(db_session),
):
    """«Примерить»: this scenario applied to one real clip, small and fast.

    The clip's own job supplies the style and the source, because a scenario
    is not a look — trying one out means seeing it on material, dressed the
    way that material is dressed.
    """
    scenario = _draft(session, scenario_id, payload)
    clip = clips.get(session, payload.clip_id)
    job = clip_jobs.get(session, clip.job_id)
    style = styles.resolved_for(
        session, profile_name=job.profile, style_id=job.style_id,
    )

    output_path = rendering.preview(
        session, clip, job,
        style=style,
        # The scenario is the point of this endpoint, so it is taken as
        # written — but the style still comes from the job, and the compiler
        # reads the style for everything the scenario does not say.
        scenario=scenarios.with_style(scenario, style),
        at_sec=payload.at_sec,
        duration_sec=payload.duration_sec,
        scale=payload.scale,
    )
    log.info("previewed scenario %s on clip %s", scenario_id, payload.clip_id)
    return FileResponse(
        output_path,
        media_type="video/mp4",
        headers={"Cache-Control": "no-store"},
    )


def _draft(session: Session, scenario_id: int, payload: ScenarioPreviewRequest):
    """What to preview: the draft on screen, or the row if none was sent."""
    if payload.data is None:
        return scenarios.stored(session, scenario_id)
    try:
        return store.from_dict(payload.data)
    except store.MalformedScenario as exc:
        raise ValidationError(f"that scenario cannot be read: {exc}") from exc


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


def _inspect(row: Scenario, report: inspector.Report) -> ScenarioInspect:
    return ScenarioInspect(
        scenario_id=row.id,
        name=row.name,
        material_sec=report.material_sec,
        timeline_sec=report.timeline_sec,
        duration_sec=report.duration_sec,
        canvas_width=report.canvas.width,
        canvas_height=report.canvas.height,
        layout=report.layout,
        blocks=[
            InspectBlock(
                element_id=block.element_id,
                track_id=block.track_id,
                track_kind=block.track_kind,
                label=block.label,
                slot_kind=block.slot_kind,
                slot_tag=block.slot_tag,
                at_sec=block.at_sec,
                duration_sec=block.duration_sec,
                anchor=block.anchor,
                frame=InspectRect(
                    x=block.frame.x, y=block.frame.y,
                    width=block.frame.width, height=block.frame.height,
                    fit=block.frame.fit,
                ),
                z=block.z,
                optional=block.optional,
                placed=block.placed,
                note=block.note,
            )
            for block in report.blocks
        ],
        rules=[
            InspectRule(
                element_id=rule.element_id,
                rule=rule.rule,
                track_id=rule.track_id,
                label=rule.label,
                limit=rule.limit,
                ghosts=[
                    InspectGhost(
                        kind=ghost.kind, at_sec=ghost.at_sec,
                        duration_sec=ghost.duration_sec, source_path=ghost.source_path,
                    )
                    for ghost in rule.ghosts
                ],
            )
            for rule in report.rules
        ],
        warnings=[
            InspectWarning(
                code=warning.code, message=warning.message, element_id=warning.element_id,
            )
            for warning in report.warnings
        ],
        subtitle_count=report.subtitle_count,
        durations=list(mock.DURATIONS),
    )
