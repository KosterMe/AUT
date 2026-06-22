"""Mapping from task kind to the code that performs it.

Adding a new kind of background work means writing one handler and decorating
it — no changes to the runner, the queue, or the API.

A handler may also register an `on_failure` hook. The queue knows *that* a task
failed and whether any attempts remain; only the handler knows what that means
for the entity behind it. Without this hook a clip whose render crashed stayed
"rendering" forever, and its job never reached a terminal status.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from app.db.enums import TaskKind
from app.tasks.context import TaskContext

# Does the work and returns whatever should be recorded as the task's result.
# Raising PermanentError means "do not retry"; TaskCancelled means the user
# asked it to stop.
Handler = Callable[[TaskContext], dict]

# Called after a handler raised. `final` is True when no attempts remain, so a
# hook can distinguish "will be retried" from "this is over".
FailureHook = Callable[[TaskContext, str, bool], None]


@dataclass(frozen=True)
class HandlerSpec:
    run: Handler
    on_failure: FailureHook | None = None


_HANDLERS: dict[str, HandlerSpec] = {}


def register_handler(
    kind: TaskKind | str, *, on_failure: FailureHook | None = None
) -> Callable[[Handler], Handler]:
    def decorator(func: Handler) -> Handler:
        key = str(kind)
        existing = _HANDLERS.get(key)
        if existing is not None and existing.run is not func:
            raise RuntimeError(f"a handler for task kind '{key}' is already registered")
        _HANDLERS[key] = HandlerSpec(run=func, on_failure=on_failure)
        return func

    return decorator


def get_handler(kind: TaskKind | str) -> HandlerSpec | None:
    return _HANDLERS.get(str(kind))


def registered_kinds() -> tuple[str, ...]:
    return tuple(sorted(_HANDLERS))


def load_handlers() -> None:
    """Import every handler module so the decorators run.

    Called by entrypoints. Explicit rather than magic, so an unused-looking
    import is never mistaken for dead code and removed.
    """
    from app.tasks.handlers import (  # noqa: F401
        cleanup,
        download,
        publish,
        render,
    )


def require_handlers(kinds: Iterable[TaskKind | str]) -> None:
    """Fail fast if a worker was asked to serve a kind nobody implements."""
    missing = [str(k) for k in kinds if get_handler(k) is None]
    if missing:
        raise RuntimeError(
            f"no handler registered for task kind(s): {', '.join(missing)}. "
            f"Known kinds: {', '.join(registered_kinds()) or 'none'}"
        )
