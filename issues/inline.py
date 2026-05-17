"""Inline field handlers used by ``InlineEditView``.

Each handler exposes the templates and an ``apply`` coroutine that mutates
``issue`` in-place. The view does the actual ``await issue.asave()``.
"""
from datetime import date as date_cls

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models

from accounts.models import User
from projects.models import Epic, Sprint

from .models import IssueType, Priority

INLINE_FIELDS = {}


def register(name):
    def deco(cls):
        INLINE_FIELDS[name] = cls()
        return cls
    return deco


def _empty(v):
    return v in (None, "", "None")


def _parse_pk(raw) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError("pk inválido") from exc


class _Base:
    label = "Campo"
    form_template = "issues/_inline/text_form.html"
    display_template = "issues/_inline/text_display.html"

    async def context(self, issue):
        return {}

    async def apply(self, issue, request) -> tuple[str, str]:
        raise NotImplementedError


@register("summary")
class _SummaryField(_Base):
    label = "Resumen"
    form_template = "issues/_inline/summary_form.html"
    display_template = "issues/_inline/summary_display.html"

    async def apply(self, issue, request):
        old = issue.summary
        new = request.POST.get("summary", "").strip()
        if new:
            issue.summary = new
        return old, issue.summary


@register("description")
class _DescriptionField(_Base):
    label = "Descripción"
    form_template = "issues/_inline/description_form.html"
    display_template = "issues/_inline/description_display.html"

    async def apply(self, issue, request):
        old = issue.description
        issue.description = request.POST.get("description", "")
        return old[:80], issue.description[:80]


@register("priority")
class _PriorityField(_Base):
    label = "Prioridad"
    form_template = "issues/_inline/select_form.html"
    display_template = "issues/_inline/priority_display.html"

    async def context(self, issue):
        return {
            "field": "priority",
            "options": [p async for p in Priority.objects.all()],
            "current_id": issue.priority_id,
        }

    async def apply(self, issue, request):
        old = str(issue.priority)
        pk = _parse_pk(request.POST.get("value"))
        issue.priority = await Priority.objects.aget(pk=pk)
        return old, str(issue.priority)


@register("issue_type")
class _TypeField(_Base):
    label = "Tipo"
    form_template = "issues/_inline/select_form.html"
    display_template = "issues/_inline/issue_type_display.html"

    async def context(self, issue):
        return {
            "field": "issue_type",
            "options": [t async for t in IssueType.objects.all()],
            "current_id": issue.issue_type_id,
        }

    async def apply(self, issue, request):
        old = str(issue.issue_type)
        pk = _parse_pk(request.POST.get("value"))
        issue.issue_type = await IssueType.objects.aget(pk=pk)
        return old, str(issue.issue_type)


@register("assignee")
class _AssigneeField(_Base):
    label = "Asignado"
    form_template = "issues/_inline/select_form.html"
    display_template = "issues/_inline/assignee_display.html"

    async def context(self, issue):
        project = issue.project
        options = [
            u async for u in
            User.objects.filter(is_active=True).filter(
                models.Q(memberships__project=project) | models.Q(led_projects=project)
            ).defer("avatar").distinct().order_by("username")
        ]
        return {
            "field": "assignee",
            "options": options,
            "current_id": issue.assignee_id,
            "allow_empty": True,
            "empty_label": "Sin asignar",
        }

    async def apply(self, issue, request):
        old = str(issue.assignee or "sin asignar")
        raw = request.POST.get("value")
        if _empty(raw):
            issue.assignee = None
        else:
            pk = _parse_pk(raw)
            project = issue.project
            user = await User.objects.filter(
                pk=pk, is_active=True,
            ).filter(
                models.Q(memberships__project=project) | models.Q(led_projects=project)
            ).afirst()
            if user is None:
                raise PermissionDenied("Usuario no pertenece al proyecto.")
            issue.assignee = user
        return old, str(issue.assignee or "sin asignar")


@register("sprint")
class _SprintField(_Base):
    label = "Sprint"
    form_template = "issues/_inline/select_form.html"
    display_template = "issues/_inline/sprint_display.html"

    async def context(self, issue):
        sprints = [s async for s in issue.project.sprints.exclude(status="closed")]
        return {
            "field": "sprint",
            "options": sprints,
            "current_id": issue.sprint_id,
            "allow_empty": True,
            "empty_label": "Backlog",
        }

    async def apply(self, issue, request):
        old = str(issue.sprint or "backlog")
        raw = request.POST.get("value")
        if _empty(raw):
            issue.sprint = None
        else:
            pk = _parse_pk(raw)
            sprint = await Sprint.objects.filter(pk=pk, project=issue.project).afirst()
            if sprint is None:
                raise PermissionDenied("Sprint no pertenece al proyecto.")
            issue.sprint = sprint
        return old, str(issue.sprint or "backlog")


@register("epic")
class _EpicField(_Base):
    label = "Epic"
    form_template = "issues/_inline/select_form.html"
    display_template = "issues/_inline/epic_display.html"

    async def context(self, issue):
        epics = [e async for e in issue.project.epics.filter(done=False)]
        return {
            "field": "epic",
            "options": epics,
            "current_id": issue.epic_id,
            "allow_empty": True,
            "empty_label": "Sin epic",
        }

    async def apply(self, issue, request):
        old = str(issue.epic or "sin epic")
        raw = request.POST.get("value")
        if _empty(raw):
            issue.epic = None
        else:
            pk = _parse_pk(raw)
            epic = await Epic.objects.filter(pk=pk, project=issue.project).afirst()
            if epic is None:
                raise PermissionDenied("Epic no pertenece al proyecto.")
            issue.epic = epic
        return old, str(issue.epic or "sin epic")


TSHIRT_TO_SP = {
    "XS": 1, "S": 2, "M": 3, "L": 5, "XL": 8, "XXL": 13,
}


@register("story_points")
class _StoryPointsField(_Base):
    label = "Story points"
    form_template = "issues/_inline/tshirt_form.html"
    display_template = "issues/_inline/story_points_display.html"

    async def context(self, issue):
        return {
            "field": "story_points",
            "value": issue.story_points or "",
            "tshirt_sizes": list(TSHIRT_TO_SP.items()),
            "current_size": _sp_to_tshirt(issue.story_points),
        }

    async def apply(self, issue, request):
        old = str(issue.story_points or "—")
        raw = request.POST.get("value", "").strip().upper()
        if raw in TSHIRT_TO_SP:
            issue.story_points = TSHIRT_TO_SP[raw]
        elif raw:
            try:
                issue.story_points = max(0, int(round(float(raw))))
            except ValueError:
                pass
        else:
            issue.story_points = None
        return old, str(issue.story_points or "—")


def _sp_to_tshirt(sp):
    if not sp:
        return ""
    # Map SP back to the closest t-shirt label for highlight in the picker.
    pairs = sorted(TSHIRT_TO_SP.items(), key=lambda kv: kv[1])
    closest = min(pairs, key=lambda kv: abs(kv[1] - sp))
    return closest[0] if closest[1] == sp else ""


@register("due_date")
class _DueDateField(_Base):
    label = "Vencimiento"
    form_template = "issues/_inline/date_form.html"
    display_template = "issues/_inline/due_date_display.html"

    async def context(self, issue):
        return {"field": "due_date", "value": issue.due_date.isoformat() if issue.due_date else ""}

    async def apply(self, issue, request):
        old = issue.due_date.isoformat() if issue.due_date else "—"
        raw = request.POST.get("value", "").strip()
        if raw:
            y, m, d = (int(p) for p in raw.split("-"))
            issue.due_date = date_cls(y, m, d)
        else:
            issue.due_date = None
        return old, (issue.due_date.isoformat() if issue.due_date else "—")


@register("estimate")
class _EstimateField(_Base):
    label = "Estimación (h)"
    form_template = "issues/_inline/number_form.html"
    display_template = "issues/_inline/estimate_display.html"

    async def context(self, issue):
        hours = issue.estimate_minutes / 60 if issue.estimate_minutes else ""
        return {"field": "estimate", "value": hours}

    async def apply(self, issue, request):
        old = f"{issue.estimate_minutes / 60:.1f}" if issue.estimate_minutes else "—"
        raw = request.POST.get("value", "").strip()
        if raw:
            issue.estimate_minutes = int(float(raw) * 60)
        else:
            issue.estimate_minutes = None
        return old, (f"{issue.estimate_minutes / 60:.1f}" if issue.estimate_minutes else "—")
