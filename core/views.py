from django.db.models import Count, Q

from issues.models import Issue
from projects.models import Project

from .async_views import AsyncTemplateView
from .mixins import AsyncLoginRequiredMixin


class HomeView(AsyncLoginRequiredMixin, AsyncTemplateView):
    template_name = "core/home.html"

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        user = self.request.user
        issues_qs = (
            Issue.objects.filter(assignee=user)
            .exclude(status__category="done")
            .select_related("project", "status", "priority", "issue_type")
            .order_by("-updated_at")[:25]
        )
        ctx["my_issues"] = [i async for i in issues_qs]

        projects_qs = (
            Project.objects.filter(Q(lead=user) | Q(members=user))
            .distinct()
            .annotate(open_count=Count("issues", filter=~Q(issues__status__category="done")))
        )
        ctx["projects"] = [p async for p in projects_qs]
        return ctx
