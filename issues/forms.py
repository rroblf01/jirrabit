from django import forms

from .models import Comment, Issue, IssueType, Label, Priority, Status


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
            "description": forms.Textarea(attrs={"rows": 6}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "labels": forms.SelectMultiple(attrs={"size": 4}),
        }

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        if project is not None:
            self.fields["epic"].queryset = project.epics.all()
            self.fields["sprint"].queryset = project.sprints.all()
            self.fields["parent"].queryset = project.issues.exclude(pk=self.instance.pk or 0)


class CommentForm(forms.ModelForm):
    class Meta:
        model = Comment
        fields = ("body",)
        widgets = {"body": forms.Textarea(attrs={"rows": 3, "placeholder": "Escribe un comentario..."})}


class QuickStatusForm(forms.Form):
    status = forms.ModelChoiceField(queryset=Status.objects.all())
