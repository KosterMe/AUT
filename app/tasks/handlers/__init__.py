"""Task handlers: one module per kind of background work.

Each registers itself with `app.tasks.registry`; the runner never imports them
individually. A handler orchestrates services and adapters and returns a small
result dict — it does not touch queue bookkeeping.
"""
