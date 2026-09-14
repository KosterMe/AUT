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


# The door of §1в, and the one place in the service that knows what HTTP is.
# Everything else under `montage/` is a library AUT can call in the same
# process, which is how it still runs by default.
DOOR = MONTAGE / "service"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(MONTAGE)))
def test_no_module_reaches_for_a_database_or_a_queue(path: Path):
    """Not by name either: what crosses the seam is values, not sessions.

    A service that renders video has no rows of its own to read here. When it
    gets its own database (§2.5) it will, and this test will be the thing that
    says so out loud rather than letting a session arrive through an argument.

    **Amended at §1в, deliberately and in one place.** `montage/service/` is
    the HTTP door, so it imports a web framework; nothing else may, and the
    ban on databases and queues holds there too — the door reads values off a
    wire and calls the same functions AUT would have called, which is the only
    reason the two ways of calling cannot drift apart.
    """
    banned = {"sqlmodel", "sqlalchemy", "alembic", "fastapi"}
    if path.is_relative_to(DOOR):
        banned -= {"fastapi"}
    reached = sorted(
        name for name in imports(path) if name.split(".")[0] in banned
    )

    assert reached == [], f"{path.relative_to(MONTAGE.parent)} imports {reached}"


def test_only_the_door_knows_about_http():
    """The exception above, pinned from the other side.

    Without this the carve-out is a hole somebody widens by moving a file into
    `service/` — which would be the cheapest possible way to lose the property
    the whole split is for.
    """
    web = {"fastapi", "starlette", "httpx", "requests", "uvicorn"}
    offenders = {
        str(path.relative_to(MONTAGE.parent)): sorted(
            name for name in imports(path) if name.split(".")[0] in web
        )
        for path in SOURCES
        if not path.is_relative_to(DOOR)
    }

    assert {k: v for k, v in offenders.items() if v} == {}


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


class TestEveryCallAcrossTheSeamFits:
    """Arity, checked statically, on both sides of the door.

    Trap 45: `client.preview` took `(output_path, spec, style)` while
    `render_preview` took `(composition, output_path, spec)`, and both preview
    endpoints raised `TypeError` in production while the suite stayed green —
    because every test replaced the very function whose call was wrong.

    This checks what no amount of patching can: that each call written down
    actually fits the signature it is written against. It says nothing about
    types, and it does not need to — the bug it exists for was arity, and so
    are most of the others that survive a package boundary.
    """

    def calls(self, path: Path, modules: dict) -> list[str]:
        import inspect

        tree = ast.parse(path.read_text(encoding="utf-8"))
        problems: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if not isinstance(owner, ast.Name) or owner.id not in modules:
                continue
            target = getattr(modules[owner.id], node.func.attr, None)
            if target is None or not callable(target):
                continue
            if any(keyword.arg is None for keyword in node.keywords):
                continue  # **kwargs at the call site: nothing to check here
            if any(isinstance(argument, ast.Starred) for argument in node.args):
                continue
            try:
                signature = inspect.signature(target)
            except (TypeError, ValueError):  # pragma: no cover - builtins
                continue
            try:
                signature.bind(
                    *[inspect.Parameter.empty] * len(node.args),
                    **{keyword.arg: None for keyword in node.keywords},
                )
            except TypeError as exc:
                problems.append(f"{path.name}: {owner.id}.{node.func.attr}() — {exc}")
        return problems

    def test_the_door_fits_what_it_opens_onto(self):
        """Every call `montage.client` makes into the package behind it."""
        from montage import client
        from montage import composition as comp
        from montage import style as style_module
        from montage.render import capabilities as build_capabilities
        from montage.render import compiler
        from montage.render import probe
        from montage.scenario import builtin

        modules = {
            "comp": comp, "compiler": compiler, "probe": probe,
            "style_module": style_module, "builtin": builtin,
            "build_capabilities": build_capabilities, "scenario": client.scenario,
        }
        path = MONTAGE / "client.py"

        assert self.calls(path, modules) == []

    def test_aut_fits_the_door(self):
        """And every call AUT makes into it, which is the other half of the
        same mistake and the half that reaches a user."""
        from montage import client

        aut = MONTAGE.parent / "app"
        callers = [
            path for path in sorted(aut.rglob("*.py"))
            if "from montage import client as montage" in path.read_text(encoding="utf-8")
        ]
        assert callers, "somebody renamed the import; this test is now blind"

        problems = [
            problem for path in callers for problem in self.calls(path, {"montage": client})
        ]

        assert problems == []
