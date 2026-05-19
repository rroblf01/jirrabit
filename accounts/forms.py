import base64
import binascii

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.utils.translation import gettext_lazy as _

from .models import User

# Cap base64 payload to keep DB rows small.
MAX_AVATAR_BYTES = 1_500_000  # ~1.1 MB raw image
ALLOWED_AVATAR_MIME = {"image/png", "image/jpeg", "image/gif", "image/webp"}


class RegisterForm(UserCreationForm):
    email = forms.EmailField(required=True)

    class Meta:
        model = User
        fields = ("username", "email", "display_name", "password1", "password2")


class ProfileForm(forms.ModelForm):
    avatar_file = forms.ImageField(
        required=False,
        label=_("Avatar"),
        help_text=_("PNG/JPEG/GIF/WebP. Se guarda como base64 en la base de datos."),
    )
    clear_avatar = forms.BooleanField(required=False, label=_("Quitar avatar actual"))
    muted_kinds_list = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label=_("Silenciar notificaciones por tipo"),
        help_text=_("Los tipos marcados no generan correo (sí avisos en la app)."),
    )

    class Meta:
        model = User
        fields = (
            "display_name", "first_name", "last_name", "email",
            "job_title", "timezone", "language", "palette", "notify_email",
        )
        widgets = {
            "palette": forms.Select(choices=()),  # populated in __init__
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from core.palettes import palette_choices_simple
        self.fields["palette"].widget = forms.Select(choices=palette_choices_simple())
        self.fields["palette"].label = _("Paleta de colores")
        from django.conf import settings as _s
        self.fields["language"] = forms.ChoiceField(
            choices=_s.LANGUAGES, label=_("Idioma"),
        )
        self.fields["notify_email"].label = _("Recibir correos")
        self.fields["timezone"] = forms.ChoiceField(
            choices=_timezone_choices(), label=_("Zona horaria"),
        )
        self.fields["muted_kinds_list"].choices = User.NOTIFY_KINDS
        if self.instance and self.instance.pk:
            current = [m.strip() for m in (self.instance.muted_kinds or "").split(",") if m.strip()]
            self.initial["muted_kinds_list"] = current

    def save(self, commit=True):
        instance = super().save(commit=False)
        selected = self.cleaned_data.get("muted_kinds_list") or []
        instance.muted_kinds = ",".join(selected)
        if commit:
            instance.save()
        return instance

    def clean_avatar_file(self):
        f = self.cleaned_data.get("avatar_file")
        if not f:
            return None
        content_type = getattr(f, "content_type", "") or ""
        if content_type not in ALLOWED_AVATAR_MIME:
            raise forms.ValidationError(
                _("Tipo no soportado: %(ct)s.") % {"ct": content_type or _("desconocido")}
            )
        if f.size and f.size > MAX_AVATAR_BYTES:
            raise forms.ValidationError(_("La imagen supera el tamaño máximo permitido."))
        return f

    def encoded_avatar(self):
        """Return the data URL for the uploaded file, or ``None`` if no upload."""
        f = self.cleaned_data.get("avatar_file")
        if not f:
            return None
        try:
            payload = f.read()
        finally:
            if hasattr(f, "seek"):
                try:
                    f.seek(0)
                except Exception:
                    pass
        try:
            encoded = base64.b64encode(payload).decode("ascii")
        except binascii.Error as exc:
            raise forms.ValidationError(str(exc)) from exc
        return f"data:{f.content_type};base64,{encoded}"


def _timezone_choices():
    import zoneinfo
    return [(t, t) for t in sorted(zoneinfo.available_timezones())]
