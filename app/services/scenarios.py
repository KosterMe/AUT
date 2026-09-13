"""Scenarios: the montages a job can be rendered with.

Four of them ship with the service and the rest are the operator's. The
difference that matters is what happens when somebody edits one: a built-in
is copied rather than changed, because the four are what every existing job
points at, and an edit that silently restyles a hundred finished jobs is not
an edit — it is a migration nobody asked for and nobody can see.
"""
from __future__ import annotations

import dataclasses
import logging

from sqlmodel import Session, col, select

from app.core.clock import utc_now
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.jsonutil import dumps, loads_dict
from app.db.models import ClipJob, Scenario as ScenarioRow
from montage import scenario as sc
from montage import style as style_module
from montage.scenario import builtin, store

log = logging.getLogger(__name__)

# What each built-in is for, shown where one is chosen.
DESCRIPTIONS = {
    "talking": "Подкасты и интервью: режем по концам фраз, паузы убираем, "
               "b-roll поверх речи.",
    "plain": "Те же склейки, ничего не добавлено и ничего не вырезано.",
    "split": "Говорящий сверху, подложка снизу. Без b-roll: нижняя половина "
             "уже держит взгляд.",
    "film": "Фильмы, шоу и спорт: режем там, где режет картинка, ранжируем по "
            "громкости.",
}


def seed(session: Session) -> list[ScenarioRow]:
    """Make sure the four built-ins exist, and bring them up to date.

    Run at startup. A built-in is code, not data: its row is a cache of what
    `builtin.py` says, so it is rewritten when that changes. An operator's own
    scenarios are never touched here.
    """
    rows: list[ScenarioRow] = []
    for name in sorted(builtin.BUILTIN):
        scenario = builtin.for_profile(name, style_module.StyleSpec.from_settings())
        data = dumps(store.to_dict(scenario))
        row = session.exec(
            select(ScenarioRow).where(ScenarioRow.name == name)
        ).first()
        if row is None:
            row = ScenarioRow(name=name, builtin=True, description=DESCRIPTIONS.get(name, ""))
            session.add(row)
        elif not row.builtin:
            # Somebody's own scenario is sitting on the name. Theirs wins: it
            # is data and this is code, and code can be renamed.
            log.warning("scenario %r is not a built-in; leaving it alone", name)
            rows.append(row)
            continue
        row.data_json = data
        row.description = DESCRIPTIONS.get(name, row.description)
        row.updated_at = utc_now()
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def get(session: Session, scenario_id: int) -> ScenarioRow:
    row = session.get(ScenarioRow, scenario_id)
    if row is None:
        raise NotFoundError(f"scenario {scenario_id} not found")
    return row


def by_name(session: Session, name: str) -> ScenarioRow | None:
    return session.exec(select(ScenarioRow).where(ScenarioRow.name == name)).first()


def list_all(session: Session) -> list[ScenarioRow]:
    """Built-ins first, then the operator's, each alphabetically."""
    return list(session.exec(
        select(ScenarioRow).order_by(col(ScenarioRow.builtin).desc(), col(ScenarioRow.name))
    ).all())


def compiled(session: Session, scenario_id: int, style: style_module.StyleSpec) -> sc.Scenario:
    """A stored scenario as the value the compiler takes.

    The style is applied over the scenario's own, because a job may carry a
    preset and a few switches of its own and those are still the last word.
    """
    row = get(session, scenario_id)
    return with_style(_read(row), style)


def stored(session: Session, scenario_id: int) -> sc.Scenario:
    """A stored scenario as it was written, style and all.

    Unlike `compiled`, nothing is laid over it: this is what the editor opens
    and what the layout view takes apart, and a scenario shown through some
    job's style would be a different scenario from the one being edited.
    """
    return _read(get(session, scenario_id))


def compiled_builtin(name: str, style: style_module.StyleSpec) -> sc.Scenario:
    """The built-in of that name, without going to the database.

    The render path still resolves a profile name this way while jobs carry
    one; a job with a `scenario_id` reads its row instead.
    """
    return builtin.for_profile(name, style)


def for_job(session: Session, job, style: style_module.StyleSpec) -> sc.Scenario:
    """The montage this job renders with.

    A job that names a scenario reads its row; one that only names a profile
    — every job created before this existed — gets the built-in of that name,
    from code rather than from its row, because the row is a cache of the code
    and a database that has not been seeded yet still has to render.

    A named scenario that is gone or unreadable falls back to the profile
    instead of failing. A render that dies because somebody deleted a scenario
    last week is a worse answer than a render in the look the job started in:
    the clip is what the operator wants, and the fallback is visible in the
    log.
    """
    scenario_id = getattr(job, "scenario_id", None)
    if scenario_id is not None:
        try:
            return compiled(session, int(scenario_id), style)
        except (NotFoundError, ValidationError) as exc:
            log.warning(
                "job %s names scenario %s, which cannot be used (%s); "
                "falling back to the %r built-in",
                getattr(job, "id", "?"), scenario_id, exc, job.profile,
            )
    return compiled_builtin(job.profile, style)


def create(
    session: Session, *, name: str, data: dict, description: str = ""
) -> ScenarioRow:
    """Store a new scenario, refusing one that cannot be read back."""
    name = (name or "").strip()
    if not name:
        raise ValidationError("a scenario needs a name")
    if by_name(session, name) is not None:
        raise ConflictError(f"a scenario called {name!r} already exists")

    name = name[:64]
    row = ScenarioRow(
        name=name, description=(description or "")[:500],
        builtin=False, data_json=dumps(_named(_checked(data), name)),
    )
    session.add(row)
    session.flush()
    log.info("stored scenario %r as %s", name, row.id)
    return row


def update(
    session: Session,
    scenario_id: int,
    *,
    data: dict,
    name: str = "",
    description: str | None = None,
) -> ScenarioRow:
    """Save an edit — or, for a built-in, save a copy of it.

    Copying rather than refusing: the operator asked to change something and
    the answer "no" would leave them with no way to have what they asked for.
    A new row named after the original is what they meant.
    """
    row = get(session, scenario_id)
    checked = _checked(data)

    if row.builtin:
        copy_name = _free_name(session, name.strip() or f"{row.name} (копия)")
        log.info("scenario %r is built in; saved the edit as %r instead", row.name, copy_name)
        return create(
            session, name=copy_name, data=checked,
            description=description if description is not None else f"На основе «{row.name}»",
        )

    if name.strip():
        row.name = name.strip()[:64]
    if description is not None:
        row.description = description[:500]
    # One name, not two: the row's. A scenario carries its own, and the two
    # drifting apart means the thing an operator renamed still calls itself
    # something else everywhere it is logged or compiled.
    row.data_json = dumps(_named(checked, row.name))
    row.updated_at = utc_now()
    session.add(row)
    session.flush()
    return row


def delete(session: Session, scenario_id: int) -> None:
    """Remove one nobody is using.

    Two refusals, for the same reason the built-ins are copied rather than
    changed: a job points at the scenario it renders with, and a re-render
    queued next month reads it again. Deleting one out from under a job would
    either fail the render or quietly render something else — so the answer is
    no, with the count, and the operator decides what to do about it.
    """
    row = get(session, scenario_id)
    if row.builtin:
        raise ConflictError(
            f"{row.name!r} ships with the service and cannot be deleted; "
            "edit it to get a copy of your own"
        )
    used_by = len(session.exec(
        select(ClipJob.id).where(ClipJob.scenario_id == scenario_id)
    ).all())
    if used_by:
        raise ConflictError(
            f"{row.name!r} is what {used_by} job(s) render with; "
            "they would have nothing to re-render from"
        )
    session.delete(row)
    session.flush()


def _read(row: ScenarioRow) -> sc.Scenario:
    try:
        return store.from_dict(loads_dict(row.data_json))
    except store.MalformedScenario as exc:
        raise ValidationError(f"scenario {row.name!r} cannot be read: {exc}") from exc


def _named(data: dict, name: str) -> dict:
    """The scenario, saying it is called what its row says it is called."""
    return {**data, "name": name}


def _checked(data: dict) -> dict:
    """Round-trip a scenario before storing it.

    Refusing here costs one request; storing something unreadable costs every
    render that points at it afterwards.
    """
    try:
        return store.to_dict(store.from_dict(data))
    except store.MalformedScenario as exc:
        raise ValidationError(str(exc)) from exc


def with_style(scenario: sc.Scenario, style: style_module.StyleSpec) -> sc.Scenario:
    """The scenario, wearing this look instead of its own.

    A job may carry a preset and a few switches, and those are the last word:
    the scenario says what is on screen, the style says how it is dressed.
    """
    return dataclasses.replace(scenario, style=style)


def _free_name(session: Session, wanted: str) -> str:
    """`wanted`, or `wanted 2`, or whatever is not taken yet."""
    if by_name(session, wanted) is None:
        return wanted
    for suffix in range(2, 100):
        candidate = f"{wanted} {suffix}"
        if by_name(session, candidate) is None:
            return candidate
    raise ConflictError(f"too many scenarios called {wanted!r}")
