"""No name that is not there.

Two production bugs in one afternoon came from this family: `render_clip_cover`
called `subtitles.write_cover_ass_file` with the import left behind by a move,
and `client.preview` was written against a signature that had changed. Neither
was visible to the suite, because both were replaced by whatever test touched
them (trap 45), and both are visible to a parser.

One rule, F821, and deliberately only one: a check that also has opinions about
blank lines becomes a thing people turn off. This has no opinions. It says that
a name used is a name defined, which is not a matter of style.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TREES = ("app", "montage", "tests")


def ruff() -> str | None:
    """Where ruff is, including the place pip puts it for a user install."""
    found = shutil.which("ruff")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "ruff"
    return str(candidate) if candidate.exists() else None


def test_no_module_uses_a_name_that_does_not_exist():
    binary = ruff()
    if binary is None:  # pragma: no cover - depends on the machine
        pytest.skip(
            "ruff is not installed, so undefined names are not being checked. "
            "It is in requirements.txt; `pip install -r requirements.txt` turns "
            "this back on."
        )

    proc = subprocess.run(
        [binary, "check", "--select", "F821", "--quiet", *TREES],
        cwd=ROOT, capture_output=True, text=True,
    )

    assert proc.returncode == 0, (
        "a name is used that is not defined — usually an import left behind by "
        f"a move:\n{proc.stdout or proc.stderr}"
    )
