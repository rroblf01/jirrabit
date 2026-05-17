from django import forms

from core.dates import parse_due_date

from .models import Comment, Issue, IssueTemplate, Status


class IssueForm(forms.ModelForm):
    class Meta:
        model = Issue
        fields = (
            "issue_type",
            "summary",
            "description",
            "status",
            "priority",
            "assignee",
            "epic",
            "sprint",
            "parent",
            "labels",
            "story_points",
            "due_date",
        )
        widgets = {
            "description": forms.Textarea(attrs={
                "rows": 8, "data-mentions": "1",
                "data-md-preview": "1", "data-slash": "1",
                "data-md-toolbar": "1",
            }),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "labels": forms.SelectMultiple(attrs={"size": 4}),
        }

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        if project is not None:
            self.fields["epic"].queryset = project.epics.all()
            self.fields["sprint"].queryset = project.sprints.all()
            self.fields["parent"].queryset = project.issues.exclude(pk=self.instance.pk or 0)
        # Accept loose date phrases ("tomorrow", "next friday", "3d").
        self.fields["due_date"].widget = forms.TextInput(attrs={
            "placeholder": "YYYY-MM-DD, mañana, viernes, 3d…",
            "data-date-hint": "1",
        })
        self.fields["due_date"].help_text = (
            "Formatos: 2026-05-30 · mañana · viernes · 3d · +2w · fin de mes"
        )

    def clean_due_date(self):
        raw = self.cleaned_data.get("due_date")
        if not raw:
            return None
        # Django parses ``date`` already if input is ISO; only kick in when
        # the user typed something it didn't understand (came through as
        # ``str`` because we replaced the widget).
        if hasattr(raw, "year"):
            return raw
        parsed = parse_due_date(str(raw))
        if parsed is None:
            raise forms.ValidationError(
                "Fecha no reconocida. Usa YYYY-MM-DD, 'mañana', 'next friday', '3d', etc."
            )
        return parsed


class CommentForm(forms.ModelForm):
    class Meta:
        model = Comment
        fields = ("body", "is_internal")
        widgets = {
            "body": forms.Textarea(attrs={
                "rows": 3,
                "placeholder": "Escribe un comentario… (Markdown soportado, prueba /)",
                "data-mentions": "1", "data-slash": "1",
                "data-md-toolbar": "1",
            }),
            "is_internal": forms.CheckboxInput(),
        }
        labels = {"is_internal": "Nota interna (solo staff)"}


class QuickStatusForm(forms.Form):
    status = forms.ModelChoiceField(queryset=Status.objects.all())


class IssueTemplateForm(forms.ModelForm):
    class Meta:
        model = IssueTemplate
        fields = ("name", "issue_type", "summary", "description", "priority", "labels")
        widgets = {
            "description": forms.Textarea(attrs={
                "rows": 6, "data-md-toolbar": "1", "data-mentions": "1",
            }),
            "labels": forms.SelectMultiple(attrs={"size": 4}),
        }
