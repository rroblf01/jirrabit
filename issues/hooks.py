"""Notification hooks (email, history). Loaded by CoreConfig.ready()."""
import re

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse

from accounts.models import User
from core.hooks import register_hook

from .models import HistoryEntry, Status

MENTION_RE = re.compile(r"@([\w._-]+)")


def _recipients(issue, exclude=None):
    users = set()
    if issue.assignee_id:
        users.add(issue.assignee)
    if issue.reporter_id:
        users.add(issue.reporter)
    for w in issue.watchers.all():
        users.add(w)
    if exclude:
        users.discard(exclude)
    return [u.email for u in users if getattr(u, "email", "")]


def _send(subject, body, recipients):
    if not recipients:
        return
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, recipients, fail_silently=True)


@register_hook("issue.created")
def email_on_created(event, issue, **_):
    body = f"Nueva incidencia {issue.key}: {issue.summary}\nReporter: {issue.reporter}\n"
    _send(f"[{issue.key}] creada — {issue.summary}", body, _recipients(issue))


@register_hook("issue.status_changed")
def email_on_status(event, issue, previous_status_id=None, **_):
    prev = Status.objects.filter(pk=previous_status_id).first()
    body = f"{issue.key} cambió de estado: {prev} -> {issue.status}\n"
    _send(f"[{issue.key}] estado: {issue.status}", body, _recipients(issue))
    HistoryEntry.objects.create(
        issue=issue,
        actor=issue.reporter,
        field="status",
        old_value=str(prev or ""),
        new_value=str(issue.status),
    )


@register_hook("issue.assigned")
def email_on_assigned(event, issue, previous_assignee_id=None, **_):
    if not issue.assignee_id:
        return
    body = f"Te asignaron {issue.key}: {issue.summary}\n"
    _send(f"[{issue.key}] asignada a ti", body, [issue.assignee.email] if issue.assignee.email else [])


@register_hook("issue.commented")
def notify_mentions(event, issue, comment, actor=None, **_):
    mentioned = set(MENTION_RE.findall(comment.body))
    if not mentioned:
        recipients = _recipients(issue, exclude=actor)
    else:
        recipients = [
            u.email for u in User.objects.filter(username__in=mentioned) if u.email
        ]
    body = f"{actor} comentó en {issue.key}:\n\n{comment.body}\n"
    _send(f"[{issue.key}] nuevo comentario", body, recipients)


@register_hook("sprint.started")
def email_sprint_started(event, sprint, **_):
    members = sprint.project.members.all()
    recipients = [u.email for u in members if u.email]
    _send(
        f"Sprint iniciado: {sprint.name}",
        f"El sprint {sprint.name} ({sprint.project.key}) ha comenzado. Meta: {sprint.goal}",
        recipients,
    )


@register_hook("issue.*")
def log_to_history_generic(event, issue=None, **_):
    """Catch-all: keep no extra entries here; per-field hooks above do it."""
    return None
