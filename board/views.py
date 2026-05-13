from django.http import Http404, HttpResponseBadRequest
from django.utils import timezone
from django.views import View

from core.aio import arender
from core.async_views import AsyncTemplateView
from core.mixins import AsyncLoginRequiredMixin
from issues.models import Issue, Status
from projects.models import Project


async def _aget_project(key):
    try:
        return await Project.objects.aget(key=key)
    except Project.DoesNotExist as exc:
        raise Http404(f"Project '{key}' not found") from exc


class BoardView(AsyncLoginRequiredMixin, AsyncTemplateView):
    template_name = "board/board.html"

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        project = await _aget_project(self.kwargs["key"])
        sprint_id = self.request.GET.get("sprint")
        if sprint_id:
            issues_qs = project.issues.filter(sprint_id=sprint_id)
            active_sprint = await project.sprints.filter(pk=sprint_id).afirst()
        else:
            active_sprint = await project.sprints.filter(status="active").afirst()
            issues_qs = (
                project.issues.filter(sprint=active_sprint)
                if active_sprint
                else project.issues.all()
            )
        issues_qs = issues_qs.select_related("status", "priority", "issue_type", "assignee")
        statuses = [s async for s in Status.objects.all()]
        all_issues = [i async for i in issues_qs.order_by("rank", "-updated_at")]
        ctx["project"] = project
        ctx["columns"] = [
            {"status": s, "issues": [i for i in all_issues if i.status_id == s.pk]}
            for s in statuses
        ]
        ctx["active_sprint"] = active_sprint
        ctx["sprints"] = [s async for s in project.sprints.all()]
        return ctx


class MoveCardView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key):
        try:
            issue = await Issue.objects.aget(key=key)
        except Issue.DoesNotExist as exc:
            raise Http404(f"Issue '{key}' not found") from exc
        status_id = request.POST.get("status")
        if not status_id:
            return HttpResponseBadRequest("status required")
        try:
            status = await Status.objects.aget(pk=status_id)
        except Status.DoesNotExist as exc:
            raise Http404(f"Status {status_id} not found") from exc
        issue.status = status
        if status.category == "done" and not issue.resolved_at:
            issue.resolved_at = timezone.now()
        elif status.category != "done":
            issue.resolved_at = None
        await issue.asave()
        return await arender(request, "board/_card.html", {"issue": issue})
