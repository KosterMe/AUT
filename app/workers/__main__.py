"""Worker entrypoint.

    python -m app.workers                       # serve every kind of task
    python -m app.workers --kinds render        # only render clips
    python -m app.workers --kinds publish --concurrency 2

One command, one process, whatever the deployment. The previous version could
run the same work three different ways — a scheduler container, an in-process
FastAPI thread toggled by `TIKTOK_EMBEDDED_SCHEDULER`, and in-process autoclip
threads toggled by another flag — and the compose file had to warn you not to
enable two at once or uploads would double-fire. Now scaling is
`docker compose up --scale worker-render=3`.

Which kinds to run where matters in practice: `render` is CPU-bound ffmpeg,
`download` is bandwidth plus a heavy ASR pass, and `publish` is mostly waiting
on TikTok. Separating them keeps a queue of renders from starving a publish
that has a scheduled time to hit.
"""
from __future__ import annotations

import argparse
import logging
import signal
import threading

from app.core.config import get_settings, load_dotenv_for_entrypoint
from app.core.logging import configure_logging
from app.db.enums import TaskKind
from app.tasks import runner
from app.tasks.registry import load_handlers, registered_kinds, require_handlers

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.workers",
        description="Run background workers for the task queue.",
    )
    parser.add_argument(
        "--kinds",
        default="",
        help=(
            "Comma-separated task kinds to serve "
            f"({', '.join(k.value for k in TaskKind)}). Default: all of them."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Tasks this process runs at once. Default: QUEUE_CONCURRENCY.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single task and exit. Useful for cron-style or one-off runs.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    load_dotenv_for_entrypoint()
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)
    settings.paths.ensure()

    load_handlers()
    kinds = _parse_kinds(args.kinds)
    if kinds:
        require_handlers(kinds)

    if args.once:
        did_work = runner.run_once(kinds)
        log.info("ran one cycle; %s", "a task was processed" if did_work else "queue was empty")
        return 0

    concurrency = max(1, args.concurrency or settings.queue.concurrency)
    stop = threading.Event()
    _install_signal_handlers(stop)

    log.info(
        "starting %d worker thread(s) for kinds=[%s]",
        concurrency,
        ",".join(kinds) if kinds else "all",
    )
    threads = [
        threading.Thread(
            target=runner.run_forever,
            kwargs={"kinds": kinds, "stop": stop},
            name=f"worker-{index + 1}",
            daemon=False,
        )
        for index in range(concurrency)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return 0


def _parse_kinds(raw: str) -> list[str]:
    kinds = [part.strip() for part in raw.split(",") if part.strip()]
    known = set(registered_kinds())
    unknown = [kind for kind in kinds if kind not in known]
    if unknown:
        raise SystemExit(
            f"unknown task kind(s): {', '.join(unknown)}. Known: {', '.join(sorted(known))}"
        )
    return kinds


def _install_signal_handlers(stop: threading.Event) -> None:
    """Stop after the current task rather than in the middle of one.

    Killing a worker mid-publish is exactly the situation the queue's
    irreversible-step guard exists for; letting it finish avoids it entirely.
    """

    def handle(signum, _frame):
        log.info("received signal %s; finishing the current task then exiting", signum)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle)
        except (ValueError, OSError):  # pragma: no cover - non-main thread / platform
            pass


if __name__ == "__main__":
    raise SystemExit(main())
