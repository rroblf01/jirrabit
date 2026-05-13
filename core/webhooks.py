"""Outgoing webhook delivery.

When a tracked model fires ``post_save``, we build a JSON payload and
hand it off to ``core.worker.enqueue`` so the HTTP delivery doesn't block
the user request. Failed deliveries record the status code / error on the
``Webhook`` row.
"""
import hashlib
import hmac
import json
import logging
import urllib.request

from asgiref.sync import sync_to_async
from django.utils import timezone

from . import worker

logger = logging.getLogger("jirrabit.webhooks")


def _serialize_issue(issue) -> dict:
    return {
        "key": issue.key,
        "summary": issue.summary,
        "status": str(issue.status),
        "priority": str(issue.priority),
        "type": str(issue.issue_type),
        "assignee": getattr(issue.assignee, "username", None),
        "reporter": getattr(issue.reporter, "username", None),
        "story_points": issue.story_points,
        "sprint": getattr(issue.sprint, "name", None),
        "epic": getattr(issue.epic, "name", None),
        "url": f"/issues/{issue.key}/",
    }


async def deliver(webhook_id: int, event: str, payload: dict) -> None:
    """Send the JSON payload to ``webhook.url`` off the event loop."""
    from projects.models import Webhook

    try:
        wh = await Webhook.objects.aget(pk=webhook_id, active=True)
    except Webhook.DoesNotExist:
        return

    body = json.dumps({"event": event, "data": payload}).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Jirrabit-Event": event}
    if wh.secret:
        sig = hmac.new(wh.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Jirrabit-Signature"] = f"sha256={sig}"

    def _post() -> tuple[int, str]:
        req = urllib.request.Request(wh.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, ""
        except urllib.error.HTTPError as e:
            return e.code, e.reason
        except Exception as e:  # network errors
            return 0, str(e)

    status, err = await sync_to_async(_post, thread_sensitive=False)()
    wh.last_status = status
    wh.last_error = err[:500]
    wh.last_delivered_at = timezone.now()
    await wh.asave(update_fields=["last_status", "last_error", "last_delivered_at"])


def fan_out_event(event: str, project, payload: dict) -> None:
    """Enqueue delivery to every active webhook listening to ``event``."""
    from projects.models import Webhook

    qs = Webhook.objects.filter(active=True)
    if project is not None:
        qs = qs.filter(project=project) | qs.filter(project__isnull=True)
    for wh in qs:
        if wh.listens_to(event):
            worker.enqueue(deliver, wh.pk, event, payload)


# ---- signal wiring ----

def _on_issue_save(sender, instance, created, **kwargs):
    event = "issue.created" if created else "issue.updated"
    fan_out_event(event, instance.project, _serialize_issue(instance))


def _on_comment_save(sender, instance, created, **kwargs):
    if not created:
        return
    fan_out_event(
        "issue.commented",
        instance.issue.project,
        {
            "issue": instance.issue.key,
            "author": getattr(instance.author, "username", None),
            "body": instance.body,
        },
    )


def connect() -> None:
    from django.db.models.signals import post_save
    from issues.models import Comment, Issue

    post_save.connect(_on_issue_save, sender=Issue, dispatch_uid="webhook_issue", weak=False)
    post_save.connect(_on_comment_save, sender=Comment, dispatch_uid="webhook_comment", weak=False)
