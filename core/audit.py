"""Project-wide audit log.

Captures any save on tracked models as an ``issues.AuditEntry`` row scoped
to the relevant project. Plays nice with the in-app notifications module —
audit is the *historical record*, notifications are the *unread inbox*.
"""

import logging
import threading

from django.db import DatabaseError
from django.db.models.signals import post_delete, post_save, pre_delete

logger = logging.getLogger("jirrabit.audit")


# Project pks whose delete cascade is currently in flight, per thread.
#
# Deleting a project cascades to its issues, comments and audit rows. Those
# children's post_delete receivers run *before* the project row is gone, so
# "does the project still exist?" cannot detect the situation — the row is
# there, the insert succeeds, and the failure arrives at commit time: the
# collector had already decided which audit rows to cascade away, and these new
# ones were not in that set. Postgres reports it as a foreign key violation on
# insert and marks the transaction failed; SQLite reports it at COMMIT. Either
# way, deleting any project that had issues in it returned 500.
#
# Catching the IntegrityError does not help — in Postgres the transaction is
# already poisoned, so the COMMIT fails regardless.
#
# The mark is keyed by project pk and held in thread-local storage, so a leak is
# bounded: a pk that lingers belongs to a project that is gone, and Postgres does
# not reuse sequence values. post_save discards the mark as well, so a delete
# that rolled back cannot leave a live project permanently unaudited.
_DELETING_PROJECTS = threading.local()


def _project_going_away(project_pk) -> bool:
    return project_pk in getattr(_DELETING_PROJECTS, "pks", ())


def _mark_project(pk, going: bool) -> None:
    pks = set(getattr(_DELETING_PROJECTS, "pks", ()))
    if going:
        pks.add(pk)
    else:
        pks.discard(pk)
    _DELETING_PROJECTS.pks = pks


def _on_project_pre_delete(sender, instance, **kwargs):
    _mark_project(instance.pk, True)


def _on_project_post_delete(sender, instance, **kwargs):
    _mark_project(instance.pk, False)


def _on_project_post_save(sender, instance, **kwargs):
    # Clears a mark left behind by a delete that rolled back.
    _mark_project(instance.pk, False)


_TRACKED = {
    "Issue",
    "Comment",
    "Attachment",
    "Epic",
    "Sprint",
    "Project",
    "WorkLog",
    "IssueLink",
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
    if _project_going_away(project.pk):
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

    if _project_going_away(project.pk):
        return

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
        logger.debug(
            "AuditEntry delete-row failed for %s pk=%s (cascade?)",
            sender.__name__,
            instance.pk,
            exc_info=True,
        )


def connect() -> None:
    from issues.models import Attachment, Comment, Issue, IssueLink, WorkLog
    from projects.models import Epic, Project, Sprint

    for model in (Issue, Comment, Attachment, Epic, Sprint, Project, WorkLog, IssueLink):
        post_save.connect(_audit_save, sender=model, dispatch_uid=f"audit_save_{model.__name__}", weak=False)
        post_delete.connect(
            _audit_delete, sender=model, dispatch_uid=f"audit_delete_{model.__name__}", weak=False
        )

    # Deleting a project takes its whole audit trail with it, so the audit rows
    # its children would write are worthless the moment they are created. See
    # _DELETING_PROJECTS for why that needs its own bookkeeping.
    pre_delete.connect(_on_project_pre_delete, sender=Project, dispatch_uid="audit_project_pre", weak=False)
    post_delete.connect(
        _on_project_post_delete, sender=Project, dispatch_uid="audit_project_post", weak=False
    )
    post_save.connect(_on_project_post_save, sender=Project, dispatch_uid="audit_project_save", weak=False)
