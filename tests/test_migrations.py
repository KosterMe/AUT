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
from sqlalchemy import create_engine, inspect
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
