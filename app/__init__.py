"""TikTok auto-poster.

Layers, outermost first:

    app.api        HTTP surface (FastAPI routers + schemas). No business logic.
    app.workers    process entrypoints for background queues.
    app.services   use cases: orchestrate domain + adapters + persistence.
    app.domain     pure logic (captions, slice planning, subtitles). No IO.
    app.adapters   the outside world (TikTok, YouTube, ffmpeg, ASR).
    app.tasks      the task queue that carries work between processes.
    app.db         SQLModel tables and session handling.
    app.core       configuration and cross-cutting primitives.

Dependencies point inwards only: api -> services -> domain/adapters -> db/core.
Nothing in `domain` may import `db`, `api` or `adapters`.
"""

__version__ = "2.0.0"
