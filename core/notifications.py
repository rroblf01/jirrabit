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
    import smtplib
    recipients = [r for r in recipients if r]
    if not recipients:
        return
    try:
        send_mail(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            recipients,
            fail_silently=False,
        )
    except (smtplib.SMTPException, OSError):
        logger.exception("Failed to send notification email to %s", recipients)


def _action(created: bool) -> str:
    return "creado" if created else "actualizado"


def _user_accepts(user, kind: str) -> bool:
    if not user.email:
        return False
    if not getattr(user, "notify_email", True):
        return False
    muted = (getattr(user, "muted_kinds", "") or "").split(",")
    return kind not in (m.strip() for m in muted if m.strip())


def _issue_recipients(issue, kind: str) -> list[str]:
    users = []
    if issue.assignee_id:
        users.append(issue.assignee)
    if issue.reporter_id:
        users.append(issue.reporter)
    users.extend(issue.watchers.all())
    seen, emails = set(), []
    for u in users:
        if u.pk in seen:
            continue
        seen.add(u.pk)
        if _user_accepts(u, kind):
            emails.append(u.email)
    return emails


def _project_recipients(project, kind: str) -> list[str]:
    users = []
    if project.lead_id:
        users.append(project.lead)
    users.extend(project.members.all())
    seen, emails = set(), []
    for u in users:
        if u.pk in seen:
            continue
        seen.add(u.pk)
        if _user_accepts(u, kind):
            emails.append(u.email)
    return emails


def _on_issue(sender, instance, created, **kwargs):
    kind = "assigned" if created else "status"
    _send(
        f"[{instance.key}] {_action(created)} — {instance.summary}",
        f"Incidencia {instance.key} {_action(created)}.\n"
        f"Resumen: {instance.summary}\n"
        f"Estado: {instance.status}\n"
        f"Prioridad: {instance.priority}\n"
        f"Asignado a: {instance.assignee or 'sin asignar'}\n",
        _issue_recipients(instance, kind),
    )
    if instance.assignee_id:
        from accounts.models import Notification
        Notification.objects.create(
            recipient_id=instance.assignee_id,
            actor=instance.reporter if created else None,
            kind=kind,
            text=f"{instance.key} {_action(created)}: {instance.summary}",
            url=f"/issues/{instance.key}/",
        )


def _on_comment(sender, instance, created, **kwargs):
    issue = instance.issue
    _send(
        f"[{issue.key}] comentario {_action(created)}",
        f"{instance.author} {_action(created)} un comentario en {issue.key}:\n\n{instance.body}\n",
        _issue_recipients(issue, "comment"),
    )
    if created:
        _create_in_app_notifications_for_comment(instance)


def _create_in_app_notifications_for_comment(comment):
    """Create ``accounts.Notification`` rows for watchers and @mentions."""
    from django.utils import timezone

    from accounts.models import Notification, User
    from core.markdown import extract_mentions
    from issues.models import NotificationSnooze

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

    # Snoozed users: skip in-app notifications until ``until`` expires.
    snoozed_ids = set(
        NotificationSnooze.objects.filter(
            issue=issue, until__gt=timezone.now(),
        ).values_list("user_id", flat=True)
    )

    url = f"/issues/{issue.key}/"
    body = comment.body[:140]
    for user_id in recipients:
        if user_id in snoozed_ids:
            continue
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
        if user_id in snoozed_ids:
            continue
        Notification.objects.create(
            recipient_id=user_id,
            actor=comment.author,
            kind="mention",
            text=f"{comment.author} te mencionó en {issue.key}",
            url=url,
        )

    # Read receipts: one row per @mention to track if/when the recipient sees it.
    from accounts.models import MentionReceipt
    for user_id in mentioned_ids - {comment.author_id}:
        MentionReceipt.objects.get_or_create(
            mentioned_id=user_id, comment=comment,
            defaults={"actor": comment.author},
        )


def _on_attachment(sender, instance, created, **kwargs):
    issue = instance.issue
    _send(
        f"[{issue.key}] adjunto {_action(created)}",
        f"{instance.uploaded_by} {_action(created)} un adjunto en {issue.key}: {instance.filename}\n",
        _issue_recipients(issue, "watch"),
    )


def _on_project(sender, instance, created, **kwargs):
    _send(
        f"Proyecto {instance.key} {_action(created)}",
        f"Proyecto {instance.key} — {instance.name} {_action(created)}.\n",
        _project_recipients(instance, "watch"),
    )


def _on_epic(sender, instance, created, **kwargs):
    project = instance.project
    _send(
        f"[{project.key}] epic {_action(created)} — {instance.name}",
        f"Epic «{instance.name}» {_action(created)} en {project.key}.\n",
        _project_recipients(project, "watch"),
    )


def _on_sprint(sender, instance, created, **kwargs):
    project = instance.project
    _send(
        f"[{project.key}] sprint {_action(created)} — {instance.name}",
        f"Sprint «{instance.name}» {_action(created)} en {project.key}. Estado: {instance.get_status_display()}.\n",
        _project_recipients(project, "watch"),
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
