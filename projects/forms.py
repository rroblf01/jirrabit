from django import forms

from .models import Epic, Project, Sprint


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ("key", "name", "description", "lead", "members")
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "members": forms.SelectMultiple(attrs={"size": 6}),
        }


class EpicForm(forms.ModelForm):
    class Meta:
        model = Epic
        fields = ("name", "summary", "color")
        widgets = {
            "summary": forms.Textarea(attrs={"rows": 3}),
            "color": forms.TextInput(attrs={"type": "color"}),
        }


class SprintForm(forms.ModelForm):
    class Meta:
        model = Sprint
        fields = ("name", "goal", "start_date", "end_date", "retro_notes")
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            "retro_notes": forms.Textarea(attrs={"rows": 4, "data-mentions": "1"}),
        }
