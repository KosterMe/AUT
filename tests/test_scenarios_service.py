"""Scenarios in the database: seeded, copied on edit, and resolved for a job.

The rule these tests are here to hold is the one in §9.2 point 4: a built-in
is code, and an edit to one makes a copy. Without it the first casual tweak to
`talking` restyles every job that ever pointed at it — a migration nobody
asked for, nobody approved, and nobody can see afterwards.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.db.models import Scenario as ScenarioRow
from app.services import clip_jobs, scenarios
from montage.scenario import builtin, model, store
from montage.style import StyleSpec


def style() -> StyleSpec:
    return StyleSpec.from_settings()


def a_scenario(name: str = "mine") -> dict:
    """A minimal but real scenario, as an editor would send it."""
    return store.to_dict(model.Scenario(
        name=name,
        tracks=(model.Track(id="spine", kind=model.TRACK_SPINE, elements=(
            model.Element(id="clip", slot=model.Slot(kind=model.SLOT_SOURCE)),
        )),),
    ))


class TestSeeding:
    def test_it_puts_the_four_built_ins_in(self, db):
        rows = scenarios.seed(db)

        assert {row.name for row in rows} == set(builtin.BUILTIN)
        assert all(row.builtin for row in rows)
        assert all(row.description for row in rows), "each one says what it is for"

    def test_running_it_again_changes_nothing(self, db):
        first = {row.name: row.id for row in scenarios.seed(db)}

        again = {row.name: row.id for row in scenarios.seed(db)}

        assert again == first
        assert db.query(ScenarioRow).count() == len(builtin.BUILTIN)

    def test_it_rewrites_a_stale_built_in(self, db):
        """The row is a cache of what `builtin.py` says. When the code changes,
        the row is what is wrong — so startup overwrites it."""
        scenarios.seed(db)
        row = scenarios.by_name(db, "talking")
        row.data_json = json.dumps(a_scenario("talking"))
        db.add(row)
        db.flush()

        scenarios.seed(db)

        assert scenarios.by_name(db, "talking").data_json != json.dumps(a_scenario("talking"))
        assert scenarios.compiled(db, row.id, style()) == builtin.talking(style())

    def test_it_leaves_somebody_elses_scenario_alone(self, db):
        """A name collision is not a licence to overwrite the operator's work:
        theirs is data and the built-in is code, and code can be renamed."""
        mine = scenarios.create(db, name="talking", data=a_scenario("talking"))

        scenarios.seed(db)

        kept = scenarios.get(db, mine.id)
        assert kept.builtin is False
        assert kept.data_json == mine.data_json


class TestABuiltinIsCopiedNotChanged:
    def test_editing_one_saves_a_copy(self, db):
        scenarios.seed(db)
        original = scenarios.by_name(db, "talking")
        before = original.data_json

        copy = scenarios.update(db, original.id, data=a_scenario("talking"))

        assert copy.id != original.id
        assert copy.builtin is False
        assert copy.name == "talking (копия)"
        assert scenarios.get(db, original.id).data_json == before

    def test_a_second_edit_does_not_collide_with_the_first(self, db):
        scenarios.seed(db)
        original = scenarios.by_name(db, "talking")

        first = scenarios.update(db, original.id, data=a_scenario("talking"))
        second = scenarios.update(db, original.id, data=a_scenario("talking"))

        assert {first.name, second.name} == {"talking (копия)", "talking (копия) 2"}

    def test_editing_your_own_edits_it(self, db):
        mine = scenarios.create(db, name="mine", data=a_scenario())

        updated = scenarios.update(db, mine.id, data=a_scenario("changed"), name="renamed")

        assert updated.id == mine.id
        assert updated.name == "renamed"
        assert db.query(ScenarioRow).count() == 1

    def test_a_built_in_cannot_be_deleted(self, db):
        scenarios.seed(db)
        row = scenarios.by_name(db, "film")

        with pytest.raises(ConflictError, match="cannot be deleted"):
            scenarios.delete(db, row.id)

    def test_your_own_can(self, db):
        mine = scenarios.create(db, name="mine", data=a_scenario())

        scenarios.delete(db, mine.id)

        with pytest.raises(NotFoundError):
            scenarios.get(db, mine.id)


class TestOneNameNotTwo:
    """A row has a name and so does the scenario inside it. They are the same
    name, or the thing an operator renamed goes on calling itself something
    else in every log line and every compile."""

    def test_storing_one_names_it_after_its_row(self, db):
        stored = scenarios.create(db, name="Мой монтаж", data=a_scenario("whatever"))

        assert scenarios.compiled(db, stored.id, style()).name == "Мой монтаж"

    def test_renaming_one_renames_it(self, db):
        stored = scenarios.create(db, name="Мой монтаж", data=a_scenario())

        scenarios.update(db, stored.id, data=a_scenario(), name="Другой")

        assert scenarios.compiled(db, stored.id, style()).name == "Другой"

    def test_a_copy_of_a_built_in_is_called_what_the_copy_is_called(self, db):
        scenarios.seed(db)
        original = scenarios.by_name(db, "talking")

        copy = scenarios.update(db, original.id, data=a_scenario("talking"))

        assert scenarios.compiled(db, copy.id, style()).name == copy.name


class TestWhatIsRefusedOnTheWayIn:
    def test_a_scenario_that_cannot_be_read_back(self, db):
        """Refusing here costs one request. Storing it costs every render that
        points at it afterwards."""
        with pytest.raises(ValidationError):
            scenarios.create(db, name="broken", data={"tracks": ["not an object"]})

        assert db.query(ScenarioRow).count() == 0

    def test_a_name_that_is_taken(self, db):
        scenarios.create(db, name="mine", data=a_scenario())

        with pytest.raises(ConflictError):
            scenarios.create(db, name="mine", data=a_scenario())

    def test_no_name_at_all(self, db):
        with pytest.raises(ValidationError, match="name"):
            scenarios.create(db, name="  ", data=a_scenario())


class TestTheStyleIsTheJobsNotTheScenarios:
    def test_a_stored_scenario_is_compiled_with_the_style_it_is_given(self, db):
        """A scenario carries a look, and a job may carry a preset and a few
        switches of its own. The job's is the last word."""
        import dataclasses

        stored = scenarios.create(db, name="mine", data=a_scenario())
        wanted = dataclasses.replace(
            style(), subtitles=dataclasses.replace(style().subtitles, enabled=False),
        )

        assert scenarios.compiled(db, stored.id, wanted).style == wanted


class TestWhatAJobRendersWith:
    def job(self, db, **fields):
        job = clip_jobs.create(
            db, source_ref="https://youtu.be/abc", start_immediately=False, **fields,
        )
        db.flush()
        return job

    def test_a_job_that_names_one_gets_it(self, db):
        scenarios.seed(db)
        mine = scenarios.create(db, name="mine", data=a_scenario("mine"))
        job = self.job(db, scenario_id=mine.id)

        assert scenarios.for_job(db, job, style()).name == "mine"

    def test_a_job_that_names_only_a_profile_gets_the_built_in(self, db):
        """Every job made before scenarios were stored. From code rather than
        from its row, so a database that has not been seeded still renders."""
        job = self.job(db, profile="film")

        assert scenarios.for_job(db, job, style()) == builtin.film(style())

    def test_a_scenario_that_is_gone_falls_back_instead_of_failing(self, db, caplog):
        """A render that dies because somebody deleted a scenario is a worse
        answer than the look the job started in.

        The job here is a stand-in rather than a row, because the database
        will not let a real one point at a missing scenario — that is the
        first defence, and `delete` refusing is the second. This is the third,
        for the row somebody removes by hand.
        """
        job = SimpleNamespace(id=7, profile="split", scenario_id=999)

        with caplog.at_level("WARNING"):
            resolved = scenarios.for_job(db, job, style())

        assert resolved == builtin.split(style())
        assert "falling back" in caplog.text

    def test_a_scenario_a_job_uses_cannot_be_deleted(self, db):
        mine = scenarios.create(db, name="mine", data=a_scenario())
        self.job(db, scenario_id=mine.id)

        with pytest.raises(ConflictError, match="1 job"):
            scenarios.delete(db, mine.id)

    def test_and_so_does_one_that_cannot_be_read(self, db):
        mine = scenarios.create(db, name="mine", data=a_scenario())
        job = self.job(db, profile="plain", scenario_id=mine.id)
        row = scenarios.get(db, mine.id)
        row.data_json = json.dumps({"tracks": ["not an object"]})
        db.add(row)
        db.flush()

        assert scenarios.for_job(db, job, style()) == builtin.plain(style())
