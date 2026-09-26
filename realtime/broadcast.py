"""Bridge Django signals into Channels groups."""

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger("jirrabit.realtime")


def _group_for(project_key: str) -> str:
    return f"project.{project_key}"


def _send(group: str, type_: str, payload: dict) -> None:
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(group, {"type": type_, "payload": payload})
    except ConnectionError, OSError, RuntimeError:
        # Backend down (Redis), event loop not running, etc. Realtime is
        # best-effort — log and continue, the DB write already succeeded.
        logger.exception("Channels group_send failed for %s", group)


def _on_issue(sender, instance, created, **kwargs):
    from core.current_user import actor_id

    _send(
        _group_for(instance.project.key),
        "issue.event",
        {
            "key": instance.key,
            "summary": instance.summary,
            "status": str(instance.status),
            "status_id": instance.status_id,
            "priority": str(instance.priority),
            "assignee": getattr(instance.assignee, "username", None),
            "created": created,
            # Who caused it. The client compares this against its own user id and
            # stays quiet: the browser that moved the card has already drawn the
            # new position optimistically, and a "changes to refresh" banner for
            # a board you just updated yourself is just noise. Absent for writes
            # with no request behind them — a command, a shell, a background
            # task — and then every client reacts, which is the right default.
            "actor_id": actor_id(),
        },
    )


def _on_comment(sender, instance, created, **kwargs):
    if not created:
        return
    _send(
        _group_for(instance.issue.project.key),
        "comment.event",
        {
            "issue": instance.issue.key,
            "author": getattr(instance.author, "username", None),
            "body": instance.body[:200],
        },
    )


def connect() -> None:
    from django.db.models.signals import post_save

    from issues.models import Comment, Issue

    post_save.connect(_on_issue, sender=Issue, dispatch_uid="rt_issue", weak=False)
    post_save.connect(_on_comment, sender=Comment, dispatch_uid="rt_comment", weak=False)
