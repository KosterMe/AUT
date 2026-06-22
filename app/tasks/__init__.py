"""The task queue.

One queue for every kind of background work. `queue` owns the persistence
rules (claiming, leases, retries), `registry` maps a task kind to the function
that does the work, and `runner` is the loop a worker process runs.

Handlers live in `app.tasks.handlers` and are written against `TaskContext`,
so they never touch queue bookkeeping themselves.
"""
from app.tasks.context import TaskContext
from app.tasks.registry import get_handler, register_handler, registered_kinds

__all__ = ["TaskContext", "get_handler", "register_handler", "registered_kinds"]
