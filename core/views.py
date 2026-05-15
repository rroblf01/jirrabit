from django.db.models import Count, Q
from django.http import HttpResponse
from django.views import View

from issues.models import Issue
from projects.models import Project

from .async_views import AsyncTemplateView
from .mixins import AsyncLoginRequiredMixin


class HelpView(AsyncTemplateView):
    """Public help page explaining Jira-style concepts and jirrabit usage."""

    template_name = "core/help.html"


class MarkdownPreviewView(AsyncLoginRequiredMixin, View):
    """Render markdown server-side and return sanitized HTML.

    Used by the split-pane editor to show a live preview without leaking
    the markdown renderer (and its bleach config) to the client.
    """

    async def post(self, request):
        from .markdown import render_markdown
        body = request.POST.get("body", "")[:50000]
        return HttpResponse(render_markdown(body))


class HomeView(AsyncLoginRequiredMixin, AsyncTemplateView):
    """Personal dashboard: assigned, watching, recently mentioned, pinned."""

    template_name = "core/home.html"

    async def aget_context_data(self, **kwargs):
        from accounts.models import Notification
        from issues.models import Pin, Visit
        ctx = await super().aget_context_data(**kwargs)
        user = self.request.user
        common = ("project", "status", "priority", "issue_type", "assignee")

        # Assigned to me, still open
        assigned_qs = (
            Issue.objects.filter(assignee=user)
            .exclude(status__category="done")
            .select_related(*common).order_by("-updated_at")[:25]
        )
        ctx["assigned"] = [i async for i in assigned_qs]

        # Watching, open
        watching_qs = (
            Issue.objects.filter(watchers=user)
            .exclude(status__category="done")
            .exclude(assignee=user)
            .select_related(*common).order_by("-updated_at")[:15]
        )
        ctx["watching"] = [i async for i in watching_qs]

        # Recent @mentions → derived from Notification kind=mention
        mention_notifs = [
            n async for n in
            Notification.objects.filter(recipient=user, kind="mention")
            .order_by("-created_at")[:10]
        ]
        ctx["mentions"] = mention_notifs

        # Pinned issues + projects
        ctx["pinned_issues"] = [
            p.issue async for p in
            Pin.objects.filter(user=user, issue__isnull=False)
            .select_related("issue", "issue__status", "issue__project")
            .order_by("-created_at")[:10]
        ]
        ctx["pinned_projects"] = [
            p.project async for p in
            Pin.objects.filter(user=user, project__isnull=False)
            .select_related("project").order_by("-created_at")[:10]
        ]

        # Recently viewed
        ctx["recent"] = [
            v.issue async for v in
            Visit.objects.filter(user=user)
            .select_related("issue", "issue__status", "issue__project")
            .order_by("-viewed_at")[:10]
        ]

        projects_qs = (
            Project.objects.filter(Q(lead=user) | Q(members=user))
            .distinct()
            .annotate(open_count=Count("issues", filter=~Q(issues__status__category="done")))
        )
        ctx["projects"] = [p async for p in projects_qs]
        return ctx
