from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _


class User(AbstractUser):
    NOTIFY_KINDS = (
        ("assigned", _("Asignaciones")),
        ("mention", _("Menciones @")),
        ("comment", _("Comentarios")),
        ("status", _("Cambios de estado")),
        ("watch", _("Cambios en seguidos")),
    )

    display_name = models.CharField(max_length=120, blank=True)
    avatar = models.TextField(
        blank=True,
        default="",
        help_text="Avatar codificado en base64 como data URL (data:image/...;base64,...).",
    )
    job_title = models.CharField(max_length=120, blank=True)
    timezone = models.CharField(max_length=64, default="Europe/Madrid")
    palette = models.CharField(
        max_length=20,
        default="blue",
        help_text=_("Paleta de colores aplicada al renderizar la UI."),
    )
    notify_email = models.BooleanField(
        default=True,
        help_text=_("Recibir correos. Si está desactivado, sólo verás avisos en la app."),
    )
    muted_kinds = models.CharField(
        max_length=120,
        blank=True,
        default="",
        help_text=_("Tipos de notificación silenciados (separados por comas)."),
    )

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


class APIKey(models.Model):
    """Personal API token for headless integrations.

    The plaintext token is shown only at creation time; the database
    stores ``hashlib.sha256(token).hexdigest()`` so a DB leak doesn't
    expose live keys.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_keys"
    )
    name = models.CharField(max_length=80)
    prefix = models.CharField(max_length=12, db_index=True, help_text="First chars, shown in UI.")
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.name} ({self.prefix}…)"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    @staticmethod
    def hash_token(plain: str) -> str:
        import hashlib
        return hashlib.sha256(plain.encode("utf-8")).hexdigest()

    @classmethod
    def create_for(cls, *, owner, name: str) -> tuple["APIKey", str]:
        """Create a new key. Returns ``(model, plaintext_token)``.

        The plaintext token is **only** available here.
        """
        import secrets
        plain = secrets.token_urlsafe(36)
        instance = cls.objects.create(
            owner=owner,
            name=name,
            prefix=plain[:8],
            token_hash=cls.hash_token(plain),
        )
        return instance, plain

    @classmethod
    async def acreate_for(cls, *, owner, name: str) -> tuple["APIKey", str]:
        import secrets
        plain = secrets.token_urlsafe(36)
        instance = await cls.objects.acreate(
            owner=owner,
            name=name,
            prefix=plain[:8],
            token_hash=cls.hash_token(plain),
        )
        return instance, plain


class InviteToken(models.Model):
    """Signed token that lets a single anonymous user register.

    An admin creates one (``/accounts/admin/invites/``); the user follows
    ``/accounts/register/?token=<hash>`` and the token is consumed on
    success. Tokens expire after ``expires_at`` and cannot be reused.
    """

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="invites_emitted"
    )
    email = models.EmailField(blank=True, help_text="Optional hint, not enforced.")
    role = models.CharField(max_length=16, default="member")
    token = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    used_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="invites_consumed"
    )

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"invite {self.token[:8]}…"

    @property
    def is_valid(self) -> bool:
        from django.utils import timezone
        if self.used_at:
            return False
        return self.expires_at > timezone.now()


class Notification(models.Model):
    KIND_CHOICES = (
        ("mention", _("Mención")),
        ("assigned", _("Asignación")),
        ("comment", _("Comentario")),
        ("status", _("Cambio de estado")),
        ("watch", _("Cambio en seguidos")),
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
