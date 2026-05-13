from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.utils import timezone
from django.views import View

from core.aio import arender
from core.async_views import AsyncTemplateView
from core.mixins import AsyncLoginRequiredMixin
from core.permissions import aassert_can_edit, aassert_can_view
from issues.models import Issue, Status
from projects.models import Project


async def _aget_project(key):
    try:
        return await Project.objects.aget(key=key)
    except Project.DoesNotExist as exc:
        raise Http404(f"Project '{key}' not found") from exc


async def _filtered_issues_qs(project, request):
    qs = project.issues.select_related("status", "priority", "issue_type", "assignee")
    assignee = request.GET.get("assignee")
    if assignee == "me":
        qs = qs.filter(assignee=request.user)
    elif assignee == "none":
        qs = qs.filter(assignee__isnull=True)
    elif assignee and assignee.isdigit():
        qs = qs.filter(assignee_id=int(assignee))
    itype = request.GET.get("type")
    if itype and itype.isdigit():
        qs = qs.filter(issue_type_id=int(itype))
    prio = request.GET.get("priority")
    if prio and prio.isdigit():
        qs = qs.filter(priority_id=int(prio))
    epic = request.GET.get("epic")
    if epic and epic.isdigit():
        qs = qs.filter(epic_id=int(epic))
    text = request.GET.get("text", "").strip()
    if text:
        from django.db.models import Q
        qs = qs.filter(Q(summary__icontains=text) | Q(key__icontains=text))
    return qs


class BoardView(AsyncLoginRequiredMixin, AsyncTemplateView):
    template_name = "board/board.html"

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        project = await _aget_project(self.kwargs["key"])
        await aassert_can_view(self.request.user, project)
        sprint_id = self.request.GET.get("sprint")
        if sprint_id and sprint_id != "all":
            issues_qs = (await _filtered_issues_qs(project, self.request)).filter(sprint_id=sprint_id)
            active_sprint = await project.sprints.filter(pk=sprint_id).afirst()
        elif sprint_id == "all":
            issues_qs = await _filtered_issues_qs(project, self.request)
            active_sprint = None
        else:
            active_sprint = await project.sprints.filter(status="active").afirst()
            base_qs = await _filtered_issues_qs(project, self.request)
            issues_qs = base_qs.filter(sprint=active_sprint) if active_sprint else base_qs

        statuses = [s async for s in Status.objects.all()]
        all_issues = [i async for i in issues_qs.order_by("rank", "-updated_at")]
        ctx["project"] = project
        ctx["columns"] = [
            {"status": s, "issues": [i for i in all_issues if i.status_id == s.pk]}
            for s in statuses
        ]
        ctx["active_sprint"] = active_sprint
        ctx["sprints"] = [s async for s in project.sprints.all()]
        ctx["filter_assignee"] = self.request.GET.get("assignee", "")
        ctx["filter_type"] = self.request.GET.get("type", "")
        ctx["filter_priority"] = self.request.GET.get("priority", "")
        ctx["filter_epic"] = self.request.GET.get("epic", "")
        ctx["filter_text"] = self.request.GET.get("text", "")
        from issues.models import IssueType, Priority
        ctx["types"] = [t async for t in IssueType.objects.all()]
        ctx["priorities"] = [p async for p in Priority.objects.all()]
        ctx["epics"] = [e async for e in project.epics.filter(done=False)]
        ctx["members"] = [m async for m in project.members.all()]
        return ctx


class MoveCardView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key):
        try:
            issue = await Issue.objects.select_related("status", "project").aget(key=key)
        except Issue.DoesNotExist as exc:
            raise Http404(f"Issue '{key}' not found") from exc
        await aassert_can_edit(request.user, issue.project)
        status_id = request.POST.get("status")
        if not status_id:
            return HttpResponseBadRequest("status required")
        try:
            status = await Status.objects.aget(pk=status_id)
        except Status.DoesNotExist as exc:
            raise Http404(f"Status {status_id} not found") from exc
        current = issue.status
        allowed = [s async for s in current.allowed_next.all()]
        if allowed and status.pk != current.pk:
            if not any(s.pk == status.pk for s in allowed):
                return HttpResponseBadRequest(f"Transición no permitida: {current} → {status}")
        issue.status = status
        if status.category == "done" and not issue.resolved_at:
            issue.resolved_at = timezone.now()
        elif status.category != "done":
            issue.resolved_at = None
        await issue.asave()
        return await arender(request, "board/_card.html", {"issue": issue})


class BulkUpdateView(AsyncLoginRequiredMixin, View):
    """Apply the same change to multiple issues at once.

    Form fields:
      - ``keys[]``: list of issue keys
      - ``action``: ``status``/``assignee``/``sprint``/``priority``/``delete``
      - ``value``: target id (or empty for delete)
    """

    async def post(self, request, key):
        project = await _aget_project(key)
        await aassert_can_edit(request.user, project)
        keys = request.POST.getlist("keys")
        action = request.POST.get("action", "")
        value = request.POST.get("value", "").strip()
        if not keys or not action:
            return HttpResponseBadRequest("keys + action requeridos")
        qs = Issue.objects.filter(project=project, key__in=keys)
        if action == "delete":
            await qs.adelete()
        elif action == "status":
            await qs.aupdate(status_id=int(value))
        elif action == "assignee":
            await qs.aupdate(assignee_id=int(value) if value else None)
        elif action == "sprint":
            await qs.aupdate(sprint_id=int(value) if value else None)
        elif action == "priority":
            await qs.aupdate(priority_id=int(value))
        else:
            return HttpResponseBadRequest("action desconocida")
        return HttpResponse(status=204, headers={"HX-Redirect": f"/board/{project.key}/"})


class BacklogView(AsyncLoginRequiredMixin, AsyncTemplateView):
    """Backlog view: shows issues per sprint plus an unassigned section."""

    template_name = "board/backlog.html"

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        project = await _aget_project(self.kwargs["key"])
        await aassert_can_view(self.request.user, project)
        sprints = [s async for s in project.sprints.exclude(status="closed")]
        groups = []
        for s in sprints:
            issues = [i async for i in s.issues.select_related("status", "priority", "issue_type", "assignee").order_by("rank", "-updated_at")]
            groups.append({"sprint": s, "issues": issues})
        unassigned = [i async for i in project.issues.filter(sprint__isnull=True).exclude(status__category="done").select_related("status", "priority", "issue_type", "assignee").order_by("-updated_at")]
        groups.append({"sprint": None, "issues": unassigned})
        ctx["project"] = project
        ctx["groups"] = groups
        ctx["sprints"] = sprints
        return ctx


class BacklogMoveView(AsyncLoginRequiredMixin, View):
    """Move an issue to a given sprint (or backlog if ``sprint`` is empty)."""

    async def post(self, request, key):
        try:
            issue = await Issue.objects.select_related("project").aget(key=key)
        except Issue.DoesNotExist as exc:
            raise Http404 from exc
        await aassert_can_edit(request.user, issue.project)
        sprint_id = request.POST.get("sprint", "").strip()
        if sprint_id:
            from projects.models import Sprint
            issue.sprint = await Sprint.objects.aget(pk=int(sprint_id))
        else:
            issue.sprint = None
        await issue.asave()
        return await arender(request, "board/_backlog_row.html", {"i": issue})
