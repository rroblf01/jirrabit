"""Tiny in-process async task queue.

Use ``enqueue(coro_callable, *args, **kwargs)`` from anywhere; the worker
loop runs the coroutine in the background without blocking the HTTP
request. There is no persistence: queued tasks are lost on process
restart. Good enough for emails and webhook delivery in a single-process
daphne setup; swap for Celery/RQ when you outgrow it.
"""
import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger("jirrabit.worker")

_queue: asyncio.Queue | None = None
_worker_task: asyncio.Task | None = None


def _ensure_started():
    global _queue, _worker_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if _queue is None or _queue._loop is not loop:  # type: ignore[attr-defined]
        _queue = asyncio.Queue()
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
    if not _ensure_started():
        # No event loop (mgmt command, etc.). Run synchronously via asyncio.run.
        asyncio.run(coro(*args, **kwargs))
        return
    assert _queue is not None
    _queue.put_nowait((coro, args, kwargs))
