"""Migrations must describe the same schema the models do.

This guard exists because the drift actually happened: a column was added to a
model after the migration was generated, and nothing noticed — the rest of the
suite builds its schema with `create_all()`, which reads the models directly
and therefore agrees with itself no matter what the migrations say. The first
symptom was a runtime `no such column` against a freshly migrated database.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlmodel import SQLModel

from app.db import models  # noqa: F401  (registers tables on SQLModel.metadata)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def migrated_engine(tmp_path, monkeypatch):
    """A database built by running the migrations, not by create_all()."""
    url = f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}"
    monkeypatch.setenv("APP_DATABASE_URL", url)

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "app" / "db" / "migrations"))
    command.upgrade(config, "head")

    engine = create_engine(url)
    yield engine
    engine.dispose()


def test_migrations_produce_every_table(migrated_engine):
    actual = set(inspect(migrated_engine).get_table_names()) - {"alembic_version"}
    assert actual == set(SQLModel.metadata.tables)


def test_migrations_match_the_models(migrated_engine):
    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "target_metadata": SQLModel.metadata}
        )
        differences = compare_metadata(context, SQLModel.metadata)

    assert differences == [], (
        "The models and the migrations disagree. Regenerate with:\n"
        "  python -m alembic revision --autogenerate -m 'describe the change'\n"
        f"Differences: {differences}"
    )


def test_the_queue_claim_index_exists(migrated_engine):
    """The index every worker poll depends on. Without it each poll scans the
    whole tasks table, which only hurts once there is a real backlog."""
    names = {index["name"] for index in inspect(migrated_engine).get_indexes("tasks")}
    assert "ix_tasks_claim" in names
    assert "ix_tasks_lease" in names


# The revision before scenarios existed: where every job said only "talking"
# or "film" and nothing said how to cut one.
BEFORE_SCENARIOS = "4920581af4ef"


def test_an_old_job_still_cuts_the_way_its_profile_did(tmp_path, monkeypatch):
    """§9.2 point 3's acceptance criterion, and the part of it a test can
    reach: the profile said two things at once, and the half about cutting is
    written onto the job by the migration rather than re-derived at render
    time. A film that came out of this upgrade cutting on speech would be
    re-cut into different clips the next time it was run.
    """
    url = f"sqlite:///{(tmp_path / 'backfill.db').as_posix()}"
    monkeypatch.setenv("APP_DATABASE_URL", url)
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "app" / "db" / "migrations"))
    command.upgrade(config, BEFORE_SCENARIOS)

    engine = create_engine(url)
    with engine.begin() as connection:
        for profile in ("talking", "plain", "split", "film"):
            connection.execute(
                text(
                    "INSERT INTO clip_jobs (source_platform, source_ref, status, progress,"
                    " source_metadata_json, created_at, updated_at, profile,"
                    " render_options_json, transcript_settings_json)"
                    " VALUES ('youtube', :ref, 'created', 0.0, '{}', :now, :now, :profile,"
                    " '{}', '{}')"
                ),
                {"ref": f"https://youtu.be/{profile}", "now": "2026-01-01 00:00:00",
                 "profile": profile},
            )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        cutters = dict(connection.execute(text("SELECT profile, cutter FROM clip_jobs")).all())
        scenarios = connection.execute(text("SELECT scenario_id FROM clip_jobs")).scalars().all()
    engine.dispose()

    assert cutters == {
        "talking": "speech", "plain": "speech", "split": "speech", "film": "scenes",
    }
    # Left null on purpose: a job that never chose a scenario renders with the
    # built-in its profile names, which is what it rendered with yesterday.
    assert list(scenarios) == [None, None, None, None]
