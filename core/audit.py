"""Project-wide audit log.

Captures any save on tracked models as an ``issues.AuditEntry`` row scoped
to the relevant project. Plays nice with the in-app notifications module —
audit is the *historical record*, notifications are the *unread inbox*.
"""
import logging

from django.db import DatabaseError
from django.db.models.signals import post_delete, post_save

logger = logging.getLogger("jirrabit.audit")


_TRACKED = {
    "Issue", "Comment", "Attachment", "Epic", "Sprint", "Project",
    "WorkLog", "IssueLink",
}


def _project_for(instance):
    cls = instance.__class__.__name__
    if cls == "Project":
        return instance
    if cls in {"Epic", "Sprint"}:
        return instance.project
    if cls == "Issue":
        return instance.project
    if cls in {"Comment", "Attachment", "WorkLog"}:
        return instance.issue.project
    if cls == "IssueLink":
        return instance.source.project
    return None


def _audit_save(sender, instance, created, **kwargs):
    if sender.__name__ not in _TRACKED:
        return
    project = _project_for(instance)
    if project is None or project.pk is None:
        return
    from issues.models import AuditEntry
    try:
        AuditEntry.objects.create(
            project=project,
            verb="created" if created else "updated",
            target_type=sender.__name__.lower(),
            target_id=instance.pk,
            target_label=str(instance)[:255],
        )
    except DatabaseError:
        logger.exception("Failed to write AuditEntry for %s pk=%s", sender.__name__, instance.pk)


def _audit_delete(sender, instance, **kwargs):
    if sender.__name__ not in _TRACKED:
        return
    # The Project itself is going away — its audit table cascades too.
    if sender.__name__ == "Project":
        return
    project = _project_for(instance)
    if project is None or project.pk is None:
        return
    from issues.models import AuditEntry
    try:
        AuditEntry.objects.create(
            project=project,
            verb="deleted",
            target_type=sender.__name__.lower(),
            target_id=instance.pk,
            target_label=str(instance)[:255],
        )
    except DatabaseError:
        # CASCADE delete may have already removed the project — expected.
        logger.debug("AuditEntry delete-row failed for %s pk=%s (cascade?)",
                     sender.__name__, instance.pk, exc_info=True)


def connect() -> None:
    from issues.models import Attachment, Comment, Issue, IssueLink, WorkLog
    from projects.models import Epic, Project, Sprint

    for model in (Issue, Comment, Attachment, Epic, Sprint, Project, WorkLog, IssueLink):
        post_save.connect(_audit_save, sender=model, dispatch_uid=f"audit_save_{model.__name__}", weak=False)
        post_delete.connect(_audit_delete, sender=model, dispatch_uid=f"audit_delete_{model.__name__}", weak=False)
