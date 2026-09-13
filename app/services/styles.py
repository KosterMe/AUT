"""Named looks, and the one place a clip's style is assembled.

A preset is the difference between "the pipeline renders" and "the pipeline
renders the way I want": it holds whatever somebody has an opinion about and
nothing else, so a job with no preset at all still runs, unattended, with the
defaults its profile implies. That is the property worth protecting — every
knob here is opt-in, and nothing ever has to be chosen for a clip to be made.

`resolved_for` is the only function that assembles the layers, which is what
keeps the API, the download handler and the render handler from each having a
slightly different idea of what a job's style is.
"""
from __future__ import annotations

import json
import logging

from sqlmodel import Session, col, select

from app.core.clock import utc_now
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.db.models import StylePreset
from app.domain import profiles
from montage import style as style_module

log = logging.getLogger(__name__)


def list_all(session: Session) -> list[StylePreset]:
    return list(session.exec(select(StylePreset).order_by(col(StylePreset.name).asc())).all())


def get(session: Session, style_id: int) -> StylePreset:
    preset = session.get(StylePreset, style_id)
    if preset is None:
        raise NotFoundError(f"style {style_id} not found")
    return preset


def create(
    session: Session,
    *,
    name: str,
    data: dict | None = None,
    description: str = "",
    profile: str | None = None,
) -> StylePreset:
    """Store a new named look.

    The overrides are validated here rather than at render time: a preset that
    cannot be resolved should fail while somebody is looking at the form, not
    four hours later inside a worker.
    """
    preset = StylePreset(
        name=_name(session, name),
        description=(description or "").strip()[:500],
        profile=_profile(profile),
        data_json=json.dumps(_overrides(data), ensure_ascii=False),
    )
    session.add(preset)
    session.flush()
    log.info("created style %s (%s)", preset.id, preset.name)
    return preset


def update(
    session: Session,
    style_id: int,
    *,
    name: str | None = None,
    data: dict | None = None,
    description: str | None = None,
    profile: str | None = None,
) -> StylePreset:
    """Change a preset. Anything left as `None` is left alone.

    `data` replaces the overrides wholesale rather than merging into them:
    merging would make it impossible to *remove* an override, which is how
    somebody puts a field back to the default.
    """
    preset = get(session, style_id)
    if name is not None:
        preset.name = _name(session, name, excluding=style_id)
    if description is not None:
        preset.description = description.strip()[:500]
    if profile is not None:
        preset.profile = _profile(profile)
    if data is not None:
        preset.data_json = json.dumps(_overrides(data), ensure_ascii=False)
    preset.updated_at = utc_now()
    session.add(preset)
    session.flush()
    return preset


def delete(session: Session, style_id: int) -> None:
    """Remove a preset, leaving the jobs that used it pointing at nothing.

    Which is correct: those jobs already rendered with the style stored on
    their clips, and a new render of one falls back to its profile's defaults
    rather than to a look that no longer exists.
    """
    preset = get(session, style_id)
    from app.db.models import ClipJob  # local: only this function needs it

    for job in session.exec(select(ClipJob).where(ClipJob.style_id == style_id)).all():
        job.style_id = None
        session.add(job)
    session.delete(preset)
    session.flush()


def overrides_of(session: Session, style_id: int | None) -> dict:
    """The stored overrides of a preset, or nothing at all.

    A missing preset is not an error here. A job whose style was deleted must
    still render — with the defaults, and a line in the log saying why.
    """
    if not style_id:
        return {}
    preset = session.get(StylePreset, style_id)
    if preset is None:
        log.warning("job asked for style %s, which no longer exists", style_id)
        return {}
    return _loads(preset)


def resolved_for(
    session: Session,
    *,
    profile_name: str | None = None,
    style_id: int | None = None,
    overrides: dict | None = None,
) -> style_module.StyleSpec:
    """The complete style for a job: defaults, profile, preset, then the job.

    Every layer is optional. With none of them this returns exactly what the
    pipeline did before any of this existed, which is the point: configuring
    something is adding to a working default, never completing a blank form.
    """
    profile = profiles.get(profile_name)
    return style_module.resolve(
        profile=profiles.style_overrides(profile),
        preset=overrides_of(session, style_id),
        overrides=overrides,
    )


def defaults_for(profile_name: str | None = None) -> style_module.StyleSpec:
    """What a profile renders as with no preset — what the UI shows greyed in."""
    return style_module.resolve(profile=profiles.style_overrides(profiles.get(profile_name)))


def _overrides(data: dict | None) -> dict:
    try:
        return style_module.sanitise(data)
    except (ValueError, TypeError) as error:
        raise ValidationError(f"that is not a usable style: {error}") from error


def _name(session: Session, name: str, *, excluding: int | None = None) -> str:
    cleaned = " ".join((name or "").split())[:64]
    if not cleaned:
        raise ValidationError("a style needs a name")
    query = select(StylePreset).where(StylePreset.name == cleaned)
    existing = session.exec(query).first()
    if existing is not None and existing.id != excluding:
        raise ConflictError(f"a style called {cleaned!r} already exists")
    return cleaned


def _profile(profile: str | None) -> str | None:
    if not profile:
        return None
    try:
        return profiles.get(profile).name
    except ValueError as error:
        raise ValidationError(str(error)) from error


def _loads(preset: StylePreset) -> dict:
    try:
        data = json.loads(preset.data_json or "{}")
    except json.JSONDecodeError:
        log.warning("style %s holds unreadable JSON; ignoring it", preset.id)
        return {}
    return data if isinstance(data, dict) else {}
