"""Outgoing webhook delivery with exponential backoff retries.

When a tracked model fires ``post_save`` we build a JSON payload and hand
it off to :mod:`core.worker`. ``deliver`` then retries up to
``MAX_ATTEMPTS`` with exponential backoff. ``2xx`` and ``410 Gone`` are
considered terminal; ``4xx`` other than 408/429 are not retried (the
endpoint is rejecting the payload on purpose); everything else (network
errors, timeouts, 5xx, 408, 429) is retried.

Failures land on the ``Webhook`` row (``last_status``, ``last_error``)
once retries are exhausted.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request

from asgiref.sync import sync_to_async
from django.utils import timezone

from . import worker

logger = logging.getLogger("jirrabit.webhooks")

# Backoff schedule: 1s, 4s, 16s, 60s, 60s — total ~2min 21s.
RETRY_DELAYS = (1, 4, 16, 60, 60)
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
TIMEOUT_SECONDS = 10


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


def _is_retryable(status: int) -> bool:
    """``0`` = network error, retry. ``2xx`` / ``410`` = stop. Other 4xx = stop.
    Anything 5xx, 408, 429 = retry."""
    if status == 0:
        return True
    if 200 <= status < 300:
        return False
    if status == 410:
        return False
    if status in (408, 429):
        return True
    if 500 <= status < 600:
        return True
    return False


async def deliver(webhook_id: int, event: str, payload: dict) -> None:
    from projects.models import Webhook

    try:
        wh = await Webhook.objects.aget(pk=webhook_id, active=True)
    except Webhook.DoesNotExist:
        return

    body = json.dumps({"event": event, "data": payload}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Jirrabit-Event": event,
        "X-Jirrabit-Delivery": f"{webhook_id}-{int(timezone.now().timestamp())}",
    }
    if wh.secret:
        sig = hmac.new(wh.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Jirrabit-Signature"] = f"sha256={sig}"

    def _post(url: str) -> tuple[int, str]:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                return resp.status, ""
        except urllib.error.HTTPError as e:
            return e.code, e.reason
        except Exception as e:
            return 0, str(e)

    status, err = 0, ""
    for attempt in range(MAX_ATTEMPTS):
        status, err = await sync_to_async(_post, thread_sensitive=False)(wh.url)
        if not _is_retryable(status):
            break
        if attempt < len(RETRY_DELAYS):
            delay = RETRY_DELAYS[attempt]
            logger.info(
                "webhook %s attempt %d failed (%s %s); retrying in %ss",
                webhook_id, attempt + 1, status, err, delay,
            )
            await asyncio.sleep(delay)

    wh.last_status = status
    wh.last_error = err[:500]
    wh.last_delivered_at = timezone.now()
    await wh.asave(update_fields=["last_status", "last_error", "last_delivered_at"])


def fan_out_event(event: str, project, payload: dict) -> None:
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
