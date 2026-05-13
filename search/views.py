from core.async_views import AsyncTemplateView
from core.mixins import AsyncLoginRequiredMixin
from issues.models import Issue

from .jql import parse_jql


class SearchView(AsyncLoginRequiredMixin, AsyncTemplateView):
    def get_template_names(self):
        if self.request.htmx:
            return ["search/_results.html"]
        return ["search/search.html"]

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        query = self.request.GET.get("q", "").strip()
        issues = []
        error = None
        if query:
            try:
                q, order = parse_jql(query)
                qs = (
                    Issue.objects.filter(q)
                    .select_related("project", "status", "priority", "issue_type", "assignee")
                    .distinct()
                    .order_by(*(order or ["-updated_at"]))
                )
                issues = [i async for i in qs]
            except Exception as e:
                error = str(e)
        ctx["query"] = query
        ctx["issues"] = issues
        ctx["error"] = error
        return ctx
