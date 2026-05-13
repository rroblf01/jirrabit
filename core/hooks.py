"""Hook registry for jirrabit.

Register handlers per event name (e.g. 'issue.created', 'issue.status_changed')
or with wildcards ('issue.*', '*').

Usage:

    from core.hooks import register_hook

    @register_hook('issue.created')
    def notify_creator(event, **payload):
        ...

Dispatch from app code (or via signals):

    from core.hooks import dispatch
    dispatch('issue.created', issue=instance, actor=user)
"""
import fnmatch
import logging
from collections import defaultdict
from typing import Callable

logger = logging.getLogger("jirrabit.hooks")

_REGISTRY: dict[str, list[Callable]] = defaultdict(list)


def register_hook(pattern: str) -> Callable[[Callable], Callable]:
    def decorator(fn: Callable) -> Callable:
        if fn not in _REGISTRY[pattern]:
            _REGISTRY[pattern].append(fn)
        return fn

    return decorator


def unregister_hook(pattern: str, fn: Callable) -> None:
    if fn in _REGISTRY.get(pattern, []):
        _REGISTRY[pattern].remove(fn)


def registered_for(event: str) -> list[Callable]:
    matched: list[Callable] = []
    for pattern, fns in _REGISTRY.items():
        if pattern == event or fnmatch.fnmatchcase(event, pattern):
            matched.extend(fns)
    return matched


def dispatch(event: str, **payload) -> list:
    results = []
    for fn in registered_for(event):
        try:
            results.append(fn(event=event, **payload))
        except Exception:
            logger.exception("Hook %s failed for event %s", fn, event)
    return results


def list_hooks() -> dict[str, list[str]]:
    return {p: [f"{fn.__module__}.{fn.__name__}" for fn in fns] for p, fns in _REGISTRY.items()}
