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
