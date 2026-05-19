"""Maintain ``User.unread_count`` in sync with :class:`Notification`.

Counter updates use ``F()`` expressions so concurrent writes don't race.
A bulk ``aupdate(read=True)`` does not fire ``post_save`` (Django bypasses
signals on QuerySet updates), so ``NotificationMarkReadView`` recomputes
the affected counters explicitly.
"""
from django.db.models import F
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from .models import Notification, User


def _delta(user_id: int, delta: int) -> None:
    if not user_id or delta == 0:
        return
    User.objects.filter(pk=user_id).update(unread_count=F("unread_count") + delta)


@receiver(pre_save, sender=Notification, dispatch_uid="notif_track_old_read")
def _track_old_read(sender, instance, **kwargs):
    if instance.pk:
        instance._old_read = (
            sender.objects.filter(pk=instance.pk).values_list("read", flat=True).first()
        )
    else:
        instance._old_read = None


@receiver(post_save, sender=Notification, dispatch_uid="notif_apply_delta")
def _apply_delta(sender, instance, created, **kwargs):
    if created:
        if not instance.read:
            _delta(instance.recipient_id, +1)
        return
    old = getattr(instance, "_old_read", None)
    if old is None or old == instance.read:
        return
    # Transitioned read <-> unread.
    _delta(instance.recipient_id, -1 if instance.read else +1)


@receiver(post_delete, sender=Notification, dispatch_uid="notif_on_delete")
def _on_delete(sender, instance, **kwargs):
    if not instance.read:
        _delta(instance.recipient_id, -1)


def recompute(user_id: int) -> int:
    """Recalculate the cached counter from the source of truth.

    Used after bulk ``aupdate`` operations that bypass ``post_save``.
    Returns the new value.
    """
    count = Notification.objects.filter(recipient_id=user_id, read=False).count()
    User.objects.filter(pk=user_id).update(unread_count=count)
    return count
