"""Scenarios: a montage written before the video exists.

The model is in `model`, what it needs to know in `facts`, and the function
that puts the two together in `compiler`. `builtin` holds the scenarios that
ship with the service, the default one first.
"""
from __future__ import annotations

from montage.scenario.anchors import CycleError, Span, order, resolve
from montage.scenario.builtin import default
from montage.scenario.compiler import CompileWarning, compile
from montage.scenario.facts import (
    CachingProvider,
    ClipFacts,
    FactKind,
    FactProvider,
    describes,
    required_facts,
)
from montage.scenario.layout import Placement, SpineLayout, lay_out
from montage.scenario.model import *  # noqa: F401,F403  (the vocabulary itself)
from montage.scenario.model import Scenario

__all__ = [
    "CachingProvider",
    "ClipFacts",
    "CompileWarning",
    "CycleError",
    "FactKind",
    "FactProvider",
    "Placement",
    "Scenario",
    "Span",
    "SpineLayout",
    "compile",
    "default",
    "describes",
    "lay_out",
    "order",
    "required_facts",
    "resolve",
]
