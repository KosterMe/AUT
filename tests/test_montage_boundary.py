"""The montage service imports nothing from AUT.

This is the acceptance criterion for the split, and it is worth a test rather
than a code review because it is exactly the kind of rule that erodes one
convenient import at a time. Each of those imports is individually reasonable
and the sum of them is a package that cannot be lifted out — which is what
stage 1в would then discover, expensively, at the point where the calls become
HTTP.

The direction matters and only one way is checked: AUT may call the montage
service, and does. The service may not call back.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

MONTAGE = Path(__file__).resolve().parents[1] / "montage"
SOURCES = sorted(MONTAGE.rglob("*.py"))


def imports(path: Path) -> set[str]:
    """Every module this file imports, by top-level package."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def test_there_is_something_to_check():
    assert len(SOURCES) > 10


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(MONTAGE)))
def test_no_module_imports_from_aut(path: Path):
    leaks = sorted(name for name in imports(path) if name == "app" or name.startswith("app."))

    assert leaks == [], (
        f"{path.relative_to(MONTAGE.parent)} imports {leaks} from AUT. The montage "
        "service has to stand on its own before its calls become HTTP — see §13.3."
    )


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(MONTAGE)))
def test_no_module_reaches_for_a_database_or_a_queue(path: Path):
    """Not by name either: what crosses the seam is values, not sessions.

    A service that renders video has no rows of its own to read here. When it
    gets its own database (§2.5) it will, and this test will be the thing that
    says so out loud rather than letting a session arrive through an argument.
    """
    banned = {"sqlmodel", "sqlalchemy", "alembic", "fastapi"}
    reached = sorted(
        name for name in imports(path) if name.split(".")[0] in banned
    )

    assert reached == [], f"{path.relative_to(MONTAGE.parent)} imports {reached}"


def test_aut_is_the_side_that_calls():
    """The other direction is expected, and this records that it exists."""
    from app.services import rendering

    assert rendering.Library is not None


class TestTheClientIsTheDoor:
    """What AUT is supposed to call, and what it is supposed to hand over."""

    def test_everything_crossing_is_a_value(self):
        """No session, no job, no task — the test of where the seam was drawn."""
        import dataclasses

        from montage import client

        fields = {f.name: f.type for f in dataclasses.fields(client.ClipRequest)}
        assert set(fields) == {
            "source_path", "start_sec", "end_sec", "style", "speech",
            "fallback_text", "title_text", "library", "seed", "index", "scenario",
        }

    def test_the_words_arrive_as_a_fact_rather_than_being_fetched(self):
        """ASR belongs to the cutter that needs it for its own work. A renderer
        that could start Whisper itself would be the coupling this split
        removes, so the check is what the package imports and not what its
        prose says — the prose is allowed to explain the rule."""
        speech_engines = {"faster_whisper", "whisper", "torch", "ctranslate2"}
        offenders = {
            str(path.relative_to(MONTAGE.parent)): sorted(
                name for name in imports(path)
                if name.split(".")[0] in speech_engines
            )
            for path in SOURCES
        }

        assert {k: v for k, v in offenders.items() if v} == {}
