from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    display_name = models.CharField(max_length=120, blank=True)
    avatar = models.TextField(
        blank=True,
        default="",
        help_text="Avatar codificado en base64 como data URL (data:image/...;base64,...).",
    )
    job_title = models.CharField(max_length=120, blank=True)
    timezone = models.CharField(max_length=64, default="Europe/Madrid")

    def __str__(self):
        return self.display_name or self.get_full_name() or self.username

    @property
    def initials(self):
        base = self.display_name or self.get_full_name() or self.username
        parts = [p for p in base.split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()


class Notification(models.Model):
    KIND_CHOICES = (
        ("mention", "Mención"),
        ("assigned", "Asignación"),
        ("comment", "Comentario"),
        ("status", "Cambio de estado"),
        ("watch", "Cambio en seguidos"),
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="actions_emitted",
    )
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    text = models.CharField(max_length=255)
    url = models.CharField(max_length=255, blank=True)
    read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["recipient", "read", "-created_at"])]
