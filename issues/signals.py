"""Bridge Django model signals into hook registry events.

We capture pre_save state so we can emit granular events like
'issue.status_changed' and 'issue.assigned'.
"""
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from core.hooks import dispatch

from .models import Comment, Issue

_PRESAVE_CACHE: dict[int, dict] = {}


@receiver(pre_save, sender=Issue)
def _issue_pre_save(sender, instance, **kwargs):
    if not instance.pk:
        return
    try:
        old = Issue.objects.get(pk=instance.pk)
    except Issue.DoesNotExist:
        return
    _PRESAVE_CACHE[instance.pk] = {
        "status_id": old.status_id,
        "assignee_id": old.assignee_id,
        "priority_id": old.priority_id,
        "sprint_id": old.sprint_id,
        "epic_id": old.epic_id,
    }


@receiver(post_save, sender=Issue)
def _issue_post_save(sender, instance, created, **kwargs):
    if created:
        dispatch("issue.created", issue=instance)
        return
    prev = _PRESAVE_CACHE.pop(instance.pk, None)
    dispatch("issue.updated", issue=instance, previous=prev)
    if prev is None:
        return
    if prev["status_id"] != instance.status_id:
        dispatch("issue.status_changed", issue=instance, previous_status_id=prev["status_id"])
    if prev["assignee_id"] != instance.assignee_id:
        dispatch("issue.assigned", issue=instance, previous_assignee_id=prev["assignee_id"])
    if prev["priority_id"] != instance.priority_id:
        dispatch("issue.priority_changed", issue=instance, previous_priority_id=prev["priority_id"])
    if prev["sprint_id"] != instance.sprint_id:
        dispatch("issue.sprint_changed", issue=instance, previous_sprint_id=prev["sprint_id"])
    if prev["epic_id"] != instance.epic_id:
        dispatch("issue.epic_changed", issue=instance, previous_epic_id=prev["epic_id"])


@receiver(post_save, sender=Comment)
def _comment_post_save(sender, instance, created, **kwargs):
    if created:
        dispatch("issue.commented", issue=instance.issue, comment=instance, actor=instance.author)
