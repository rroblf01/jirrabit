"""Registries for webhook event types and webhook action handlers.

Two registries live here:

* **Events** — populated via :func:`webhook_event`. Each spec describes a
  trigger (e.g. "an issue changes status"). The UI uses these to populate
  the entity/event dropdowns; the dispatcher uses them to decide whether
  ``state_filter`` applies.

* **Actions** — populated via :func:`webhook_action`. Each entry is a
  callable that runs in-process when a matching event fires. This
  replaces the old outbound-HTTP design: the user picks an action from
  this list instead of pasting a URL.

Action signature::

    def my_action(event: str, payload: dict, state: str | None) -> None
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Union

ActionFn = Callable[[str, dict, "str | None"], Union[None, Awaitable[None]]]


@dataclass(frozen=True)
class EventSpec:
    code: str  # e.g. "issue.status_changed"
    label: str  # human label shown in the UI
    entity: str  # "issue" / "epic" / "comment" / ...
    state_filterable: bool = False  # whether ``state_filter`` applies
    state_resolver: Callable | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ActionSpec:
    code: str  # stable id, stored on Webhook.action
    label: str  # human label in the UI
    fn: ActionFn = field(repr=False)


_EVENTS: dict[str, EventSpec] = {}
_ACTIONS: dict[str, ActionSpec] = {}


def webhook_event(
    code: str,
    label: str,
    entity: str,
    state_filterable: bool = False,
    state_resolver: Callable | None = None,
):
    """Decorator: register an event spec; returns the wrapped function unchanged."""

    def _wrap(fn):
        _EVENTS[code] = EventSpec(
            code=code,
            label=label,
            entity=entity,
            state_filterable=state_filterable,
            state_resolver=state_resolver,
        )
        return fn

    return _wrap


def webhook_action(code: str, label: str):
    """Decorator: register an action handler the user can attach to webhooks.

    Example::

        @webhook_action("notify.slack_devops", "Avisar al canal #devops")
        def notify_slack(event, payload, state):
            ...  # arbitrary side effect
    """

    def _wrap(fn: ActionFn):
        _ACTIONS[code] = ActionSpec(code=code, label=label, fn=fn)
        return fn

    return _wrap


def all_events() -> list[EventSpec]:
    return sorted(_EVENTS.values(), key=lambda s: (s.entity, s.code))


def events_for_entity(entity: str) -> list[EventSpec]:
    return [s for s in all_events() if s.entity == entity]


def get(code: str) -> EventSpec | None:
    return _EVENTS.get(code)


def entities() -> list[str]:
    return sorted({s.entity for s in _EVENTS.values()})


def all_actions() -> list[ActionSpec]:
    return sorted(_ACTIONS.values(), key=lambda a: a.code)


def get_action(code: str) -> ActionSpec | None:
    return _ACTIONS.get(code)
