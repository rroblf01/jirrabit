"""Tiny in-process async task queue.

Use ``enqueue(coro_callable, *args, **kwargs)`` from anywhere; the worker
loop runs the coroutine in the background without blocking the HTTP
request. There is no persistence: queued tasks are lost on process
restart. Good enough for emails and webhook delivery in a single-process
daphne setup; swap for Celery/RQ when you outgrow it.

Calls from sync contexts (signals fired inside
``sync_to_async(thread_sensitive=True)`` blocks, management commands)
are forwarded to the worker loop with ``call_soon_threadsafe`` once the
loop is known. This is critical: spawning a fresh thread + event loop
for every webhook delivery used to open a brand new Postgres connection
each time, which — combined with ``CONN_MAX_AGE > 0`` — quickly drained
the database's connection slots (``FATAL: sorry, too many clients``).
"""

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("jirrabit.worker")

_queue: asyncio.Queue | None = None
_worker_task: asyncio.Task | None = None
_main_loop: asyncio.AbstractEventLoop | None = None
_lock = threading.Lock()


def _ensure_started():
    """Start the worker on the *current* running loop. Caches the loop ref."""
    global _queue, _worker_task, _main_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    with _lock:
        if _queue is None or _queue._loop is not loop:  # type: ignore[attr-defined]
            _queue = asyncio.Queue()
            _main_loop = loop
        if _worker_task is None or _worker_task.done():
            _worker_task = loop.create_task(_run())
    return True


async def _run():
    assert _queue is not None
    while True:
        coro, args, kwargs = await _queue.get()
        try:
            await coro(*args, **kwargs)
        except Exception:
            logger.exception("Background task failed")
        finally:
            _queue.task_done()


def enqueue(coro: Callable[..., Awaitable[Any]], *args, **kwargs) -> None:
    """Schedule ``coro(*args, **kwargs)`` on the worker. Fire-and-forget."""
    if _ensure_started():
        assert _queue is not None
        _queue.put_nowait((coro, args, kwargs))
        return

    # No loop in this thread. If the worker is already running on the
    # main daphne loop, hand off to it thread-safely — no new DB
    # connections, no new threads.
    if _main_loop is not None and _main_loop.is_running() and _queue is not None:
        _main_loop.call_soon_threadsafe(_queue.put_nowait, (coro, args, kwargs))
        return

    # True one-shot context (management command etc.). Run on a throwaway
    # thread with its own loop, and close DB connections after so we
    # don't leak Postgres slots.
    def _bootstrap():
        try:
            asyncio.run(coro(*args, **kwargs))
        except Exception:
            logger.exception("Background task failed")
        finally:
            try:
                from django.db import connections
                connections.close_all()
            except Exception:
                logger.exception("Failed to close connections after task")

    threading.Thread(target=_bootstrap, daemon=True).start()
