"""Webhook event emission + in-process action dispatch.

Each :class:`projects.models.Webhook` row binds:

* an **event** (from the registry — what triggered it)
* an optional **state filter** (which entity states fire it)
* an **action** (a Python callable registered via ``@webhook_action``)

When a domain model fires its ``post_save`` signal, the matching event
emitter calls :func:`fan_out_event`, which looks up every active webhook
listening to that ``(event, state)`` combo and hands off to the worker.
The worker resolves the webhook's ``action`` code against the registry
and runs the callable. Failures land on the ``Webhook`` row
(``last_status``, ``last_error``).
"""
import asyncio
import inspect
import logging

from asgiref.sync import sync_to_async
from django.utils import timezone

from . import worker
from .webhook_registry import (
    get as get_event_spec,
    get_action,
    webhook_action,
    webhook_event,
)

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


async def deliver(webhook_id: int, event: str, payload: dict, state: str | None = None) -> None:
    """Run the webhook's bound action. Any exception is logged on the row."""
    from projects.models import Webhook

    try:
        wh = await Webhook.objects.aget(pk=webhook_id, active=True)
    except Webhook.DoesNotExist:
        return

    spec = get_action(wh.action)
    if spec is None:
        wh.last_status = 0
        wh.last_error = f"action '{wh.action}' not registered"
        wh.last_delivered_at = timezone.now()
        await wh.asave(update_fields=["last_status", "last_error", "last_delivered_at"])
        logger.warning("webhook %s: action %r missing", webhook_id, wh.action)
        return

    status, err = 200, ""
    try:
        result = spec.fn(event, payload, state)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001 — surface every failure on the row
        status, err = 500, repr(exc)
        logger.exception("webhook %s: action %r raised", webhook_id, wh.action)

    wh.last_status = status
    wh.last_error = err[:500]
    wh.last_delivered_at = timezone.now()
    await wh.asave(update_fields=["last_status", "last_error", "last_delivered_at"])


def fan_out_event(event: str, project, payload: dict, current_state: str | None = None) -> None:
    from projects.models import Webhook

    qs = Webhook.objects.filter(active=True, event__in=(event, "*"))
    if project is not None:
        qs = qs.filter(project=project) | qs.filter(project__isnull=True, event__in=(event, "*"))
    for wh in qs:
        if wh.listens_to(event, current_state=current_state):
            worker.enqueue(deliver, wh.pk, event, payload, current_state)


# ---- signal wiring (event emitters) -------------------------------------

@webhook_event("issue.created", "Issue creada", entity="issue")
def _emit_issue_created(instance):
    fan_out_event("issue.created", instance.project, _serialize_issue(instance),
                  current_state=str(instance.status))


@webhook_event("issue.updated", "Issue actualizada", entity="issue")
def _emit_issue_updated(instance):
    fan_out_event("issue.updated", instance.project, _serialize_issue(instance),
                  current_state=str(instance.status))


@webhook_event(
    "issue.status_changed",
    "Issue cambia de estado",
    entity="issue",
    state_filterable=True,
)
def _emit_issue_status_changed(instance):
    fan_out_event("issue.status_changed", instance.project, _serialize_issue(instance),
                  current_state=str(instance.status))


@webhook_event("issue.commented", "Comentario nuevo", entity="comment")
def _emit_comment_created(instance):
    fan_out_event(
        "issue.commented",
        instance.issue.project,
        {
            "issue": instance.issue.key,
            "author": getattr(instance.author, "username", None),
            "body": instance.body,
        },
    )


@webhook_event("epic.created", "Epic creada", entity="epic")
def _emit_epic_created(instance):
    fan_out_event("epic.created", instance.project,
                  {"name": instance.name, "key": str(instance.pk)})


@webhook_event("epic.updated", "Epic actualizada", entity="epic")
def _emit_epic_updated(instance):
    fan_out_event("epic.updated", instance.project,
                  {"name": instance.name, "key": str(instance.pk)})


def _on_issue_pre_save(sender, instance, **kwargs):
    """Snapshot previous status_id so post_save can detect transitions."""
    if not instance.pk:
        instance._old_status_id = None
        return
    instance._old_status_id = (
        sender.objects.filter(pk=instance.pk).values_list("status_id", flat=True).first()
    )


def _on_issue_save(sender, instance, created, **kwargs):
    if created:
        _emit_issue_created(instance)
        return
    _emit_issue_updated(instance)
    old = getattr(instance, "_old_status_id", None)
    if old is not None and old != instance.status_id:
        _emit_issue_status_changed(instance)


def _on_comment_save(sender, instance, created, **kwargs):
    if not created:
        return
    _emit_comment_created(instance)


def _on_epic_save(sender, instance, created, **kwargs):
    if created:
        _emit_epic_created(instance)
    else:
        _emit_epic_updated(instance)


def connect() -> None:
    from django.db.models.signals import post_save, pre_save

    from issues.models import Comment, Issue
    from projects.models import Epic

    pre_save.connect(_on_issue_pre_save, sender=Issue, dispatch_uid="webhook_issue_pre", weak=False)
    post_save.connect(_on_issue_save, sender=Issue, dispatch_uid="webhook_issue", weak=False)
    post_save.connect(_on_comment_save, sender=Comment, dispatch_uid="webhook_comment", weak=False)
    post_save.connect(_on_epic_save, sender=Epic, dispatch_uid="webhook_epic", weak=False)


# ---- demo built-in actions ----------------------------------------------
# Stubs for demo purposes. Each one logs what it *would* do — replace the
# bodies with real integrations (SMTP, Slack, Calendar API, PagerDuty…) in
# production. Register your own with ``@webhook_action`` in any module
# loaded at startup (e.g. an app's ``ready()`` hook).

@webhook_action("log.info", "Loggear en jirrabit.webhooks (info)")
def _action_log(event, payload, state):
    logger.info("[webhook] event=%s state=%s payload=%s", event, state, payload)


@webhook_action("email.director", "Enviar correo al director")
async def _action_email_director(event, payload, state):
    from django.conf import settings
    from django.core.mail import send_mail

    key = payload.get("key") or payload.get("name", "?")
    subject = f"[Jirrabit] {event} → {key}"
    body = (
        f"Atención director,\n\n"
        f"Se ha disparado el evento '{event}'"
        f"{f' (estado: {state})' if state else ''}.\n\n"
        f"Detalle:\n"
        f"  · Issue: {key}\n"
        f"  · Resumen: {payload.get('summary', '—')}\n"
        f"  · Asignada a: {payload.get('assignee') or '—'}\n"
        f"  · Prioridad: {payload.get('priority', '—')}\n\n"
        f"— Bot de Jirrabit"
    )
    director = getattr(settings, "DIRECTOR_EMAIL", "director@example.com")
    await sync_to_async(send_mail, thread_sensitive=False)(
        subject, body, settings.DEFAULT_FROM_EMAIL, [director], fail_silently=True
    )
    logger.info("[webhook] email enviado a director (%s) sobre %s", director, key)


@webhook_action("calendar.create_meeting", "Crear reunión de revisión")
def _action_create_meeting(event, payload, state):
    from datetime import datetime, timedelta

    start = datetime.now() + timedelta(hours=24)
    end = start + timedelta(minutes=30)
    invitees = [payload.get("assignee"), payload.get("reporter")]
    invitees = [u for u in invitees if u]
    logger.info(
        "[webhook] CalendarStub: reunión '%s' creada %s → %s, invitados=%s",
        f"Revisión {payload.get('key', '?')}",
        start.isoformat(timespec="minutes"),
        end.isoformat(timespec="minutes"),
        invitees or ["—"],
    )


@webhook_action("slack.post_incidents", "Postear en #incidents (Slack)")
def _action_slack_incidents(event, payload, state):
    emoji = {"Done": ":white_check_mark:", "Blocked": ":no_entry:"}.get(state, ":warning:")
    msg = (
        f"{emoji} *{payload.get('key', '?')}* — {payload.get('summary', '')}\n"
        f"> evento: `{event}` · estado: *{state or 'n/a'}* · "
        f"asignada a: {payload.get('assignee') or '_sin asignar_'}"
    )
    logger.info("[webhook] SlackStub #incidents: %s", msg)


@webhook_action("pagerduty.page_oncall", "Despertar al oncall (PagerDuty)")
def _action_page_oncall(event, payload, state):
    severity = "critical" if (payload.get("priority", "").lower() in ("blocker", "critical")) else "warning"
    logger.warning(
        "[webhook] PagerDutyStub: incidente severity=%s · key=%s · summary=%r",
        severity, payload.get("key", "?"), payload.get("summary"),
    )


@webhook_action("jira.auto_assign_lead", "Auto-asignar al lead del proyecto")
async def _action_auto_assign_lead(event, payload, state):
    from issues.models import Issue
    from projects.models import Project

    key = payload.get("key")
    if not key:
        return
    issue = await Issue.objects.filter(key=key).only("pk", "project_id", "assignee_id").afirst()
    if not issue or not issue.project_id or issue.assignee_id:
        return
    lead = await Project.objects.filter(pk=issue.project_id).values_list("lead_id", flat=True).afirst()
    if not lead:
        return
    await Issue.objects.filter(pk=issue.pk).aupdate(assignee_id=lead)
    logger.info("[webhook] %s auto-asignada al lead del proyecto", key)


@webhook_action("release.block", "Bloquear release (registrar veto)")
def _action_block_release(event, payload, state):
    logger.error(
        "[webhook] RELEASE BLOCKED — issue=%s estado=%s prio=%s",
        payload.get("key", "?"), state, payload.get("priority", "?"),
    )


@webhook_action("kpi.snapshot", "Capturar snapshot de KPIs")
def _action_kpi_snapshot(event, payload, state):
    logger.info(
        "[webhook] KPIStub: snapshot {evento=%s, estado=%s, sprint=%s, sp=%s}",
        event, state, payload.get("sprint"), payload.get("story_points"),
    )


@webhook_action("postmortem.draft", "Generar plantilla de post-mortem")
def _action_postmortem_draft(event, payload, state):
    key = payload.get("key", "INC")
    template = (
        f"# Post-mortem {key}\n\n"
        f"**Resumen:** {payload.get('summary', '—')}\n"
        f"**Detectado:** ahora\n"
        f"**Resuelto:** —\n"
        f"**Severidad:** {payload.get('priority', '—')}\n\n"
        "## Línea de tiempo\n- \n\n## Causa raíz\n- \n\n## Acciones\n- "
    )
    logger.info("[webhook] PostmortemStub draft para %s:\n%s", key, template)