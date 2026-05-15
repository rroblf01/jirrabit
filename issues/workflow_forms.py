"""Forms for the global workflow editor.

Status, Priority, IssueType and Label live in a single global pool.
These forms power the ``/workflow/`` admin UI.
"""
from django import forms
from django.utils.translation import gettext_lazy as _

from .models import IssueType, Label, Priority, Status


class StatusForm(forms.ModelForm):
    class Meta:
        model = Status
        fields = ("name", "category", "order")
        help_texts = {
            "category": _("Categoría usada para colorear y para regla 'done → resolved_at'."),
            "order": _("Posición en columnas del board (menor = más a la izquierda)."),
        }


class StatusTransitionsForm(forms.ModelForm):
    """Edit a status's ``allowed_next`` set via checkboxes."""

    class Meta:
        model = Status
        fields = ("allowed_next",)
        widgets = {
            "allowed_next": forms.CheckboxSelectMultiple,
        }
        help_texts = {
            "allowed_next": _(
                "Estados destino permitidos. Si la lista está vacía, "
                "se permite cualquier transición desde este estado."
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["allowed_next"].queryset = Status.objects.exclude(pk=self.instance.pk)


class PriorityForm(forms.ModelForm):
    class Meta:
        model = Priority
        fields = ("name", "weight", "color")
        widgets = {"color": forms.TextInput(attrs={"type": "color"})}
        help_texts = {
            "weight": _("Más alto = más urgente. Se usa para ordenar."),
        }


class IssueTypeForm(forms.ModelForm):
    class Meta:
        model = IssueType
        fields = ("name", "category", "icon", "color", "description_template")
        widgets = {
            "color": forms.TextInput(attrs={"type": "color"}),
            "description_template": forms.Textarea(attrs={"rows": 6}),
        }
        help_texts = {
            "icon": _("Un emoji o carácter corto (≤8 chars) usado en badges."),
            "description_template": _(
                "Plantilla Markdown prerellenada al crear tareas de este tipo. "
                "Útil para bugs (pasos, esperado, real) o stories (como X quiero Y para Z)."
            ),
        }


class LabelForm(forms.ModelForm):
    class Meta:
        model = Label
        fields = ("name", "color")
        widgets = {"color": forms.TextInput(attrs={"type": "color"})}
