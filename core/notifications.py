"""Single notification hook: send one email each time a tracked model is
created or updated.

The handlers below are connected from :class:`core.apps.CoreConfig.ready`
to ``post_save`` for each tracked model. Recipients are derived from the
instance (assignee/reporter/watchers for issues, lead/members for projects,
etc.). Email backend is whatever ``EMAIL_BACKEND`` points at — console in
dev, SMTP in production.
"""
import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db.models.signals import post_save

logger = logging.getLogger("jirrabit.notifications")


def _send(subject: str, body: str, recipients: list[str]) -> None:
    recipients = [r for r in recipients if r]
    if not recipients:
        return
    try:
        send_mail(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            recipients,
            fail_silently=True,
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to send notification email")


def _action(created: bool) -> str:
    return "creado" if created else "actualizado"


def _issue_recipients(issue) -> list[str]:
    emails = set()
    if issue.assignee_id and issue.assignee.email:
        emails.add(issue.assignee.email)
    if issue.reporter_id and issue.reporter.email:
        emails.add(issue.reporter.email)
    for w in issue.watchers.all():
        if w.email:
            emails.add(w.email)
    return list(emails)


def _project_recipients(project) -> list[str]:
    emails = set()
    if project.lead_id and project.lead.email:
        emails.add(project.lead.email)
    for m in project.members.all():
        if m.email:
            emails.add(m.email)
    return list(emails)


def _on_issue(sender, instance, created, **kwargs):
    _send(
        f"[{instance.key}] {_action(created)} — {instance.summary}",
        f"Incidencia {instance.key} {_action(created)}.\n"
        f"Resumen: {instance.summary}\n"
        f"Estado: {instance.status}\n"
        f"Prioridad: {instance.priority}\n"
        f"Asignado a: {instance.assignee or 'sin asignar'}\n",
        _issue_recipients(instance),
    )
    if instance.assignee_id:
        from accounts.models import Notification
        Notification.objects.create(
            recipient_id=instance.assignee_id,
            actor=instance.reporter if created else None,
            kind="assigned" if created else "status",
            text=f"{instance.key} {_action(created)}: {instance.summary}",
            url=f"/issues/{instance.key}/",
        )


def _on_comment(sender, instance, created, **kwargs):
    issue = instance.issue
    _send(
        f"[{issue.key}] comentario {_action(created)}",
        f"{instance.author} {_action(created)} un comentario en {issue.key}:\n\n{instance.body}\n",
        _issue_recipients(issue),
    )
    if created:
        _create_in_app_notifications_for_comment(instance)


def _create_in_app_notifications_for_comment(comment):
    """Create ``accounts.Notification`` rows for watchers and @mentions."""
    from accounts.models import Notification, User
    from core.markdown import extract_mentions

    issue = comment.issue
    recipients = set()
    for w in issue.watchers.all():
        recipients.add(w.pk)
    if issue.assignee_id:
        recipients.add(issue.assignee_id)
    if issue.reporter_id:
        recipients.add(issue.reporter_id)
    recipients.discard(comment.author_id)

    mentioned_usernames = set(extract_mentions(comment.body))
    mentioned_ids = set(
        User.objects.filter(username__in=mentioned_usernames).values_list("pk", flat=True)
    )

    url = f"/issues/{issue.key}/"
    body = comment.body[:140]
    for user_id in recipients:
        kind = "mention" if user_id in mentioned_ids else "comment"
        Notification.objects.create(
            recipient_id=user_id,
            actor=comment.author,
            kind=kind,
            text=f"{comment.author} comentó en {issue.key}: {body}",
            url=url,
        )
    # Mentions for users that are NOT watchers/assignee/reporter
    for user_id in mentioned_ids - recipients - {comment.author_id}:
        Notification.objects.create(
            recipient_id=user_id,
            actor=comment.author,
            kind="mention",
            text=f"{comment.author} te mencionó en {issue.key}",
            url=url,
        )


def _on_attachment(sender, instance, created, **kwargs):
    issue = instance.issue
    _send(
        f"[{issue.key}] adjunto {_action(created)}",
        f"{instance.uploaded_by} {_action(created)} un adjunto en {issue.key}: {instance.filename}\n",
        _issue_recipients(issue),
    )


def _on_project(sender, instance, created, **kwargs):
    _send(
        f"Proyecto {instance.key} {_action(created)}",
        f"Proyecto {instance.key} — {instance.name} {_action(created)}.\n",
        _project_recipients(instance),
    )


def _on_epic(sender, instance, created, **kwargs):
    project = instance.project
    _send(
        f"[{project.key}] epic {_action(created)} — {instance.name}",
        f"Epic «{instance.name}» {_action(created)} en {project.key}.\n",
        _project_recipients(project),
    )


def _on_sprint(sender, instance, created, **kwargs):
    project = instance.project
    _send(
        f"[{project.key}] sprint {_action(created)} — {instance.name}",
        f"Sprint «{instance.name}» {_action(created)} en {project.key}. Estado: {instance.get_status_display()}.\n",
        _project_recipients(project),
    )


def _on_user(sender, instance, created, **kwargs):
    if not instance.email:
        return
    # Login bumps ``last_login`` via ``update_fields={"last_login"}``;
    # don't spam the user on every sign-in.
    update_fields = kwargs.get("update_fields")
    if update_fields and set(update_fields) == {"last_login"}:
        return
    _send(
        f"Cuenta {_action(created)}",
        f"Tu cuenta en jirrabit ha sido {_action(created)}.\n",
        [instance.email],
    )


def connect() -> None:
    """Wire all post_save signals. Idempotent thanks to ``dispatch_uid``."""
    from accounts.models import User
    from issues.models import Attachment, Comment, Issue
    from projects.models import Epic, Project, Sprint

    pairs = [
        (Issue, _on_issue, "notify_issue"),
        (Comment, _on_comment, "notify_comment"),
        (Attachment, _on_attachment, "notify_attachment"),
        (Project, _on_project, "notify_project"),
        (Epic, _on_epic, "notify_epic"),
        (Sprint, _on_sprint, "notify_sprint"),
        (User, _on_user, "notify_user"),
    ]
    for model, handler, uid in pairs:
        post_save.connect(handler, sender=model, dispatch_uid=uid, weak=False)
