from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import redirect
from django.views import View

from issues.models import Issue
from projects.models import Project

from .aio import arender
from .async_views import AsyncTemplateView
from .mixins import AsyncLoginRequiredMixin


class HelpView(AsyncTemplateView):
    """Public help page explaining Jira-style concepts and jirrabit usage."""

    template_name = "core/help.html"


class HealthzView(View):
    """Public-ish JSON health probe. Returns service availability.

    Anonymous-readable so it can be hit by load balancers / uptime checks.
    Each subsystem failure flips ``ok`` to ``false`` but the response is
    always 200 (so monitors get a structured payload to parse).
    """

    async def get(self, request):
        import json

        from asgiref.sync import sync_to_async
        from channels.layers import get_channel_layer
        from django.db import connection

        result = {"ok": True, "services": {}}

        # DB ping
        try:
            await sync_to_async(connection.ensure_connection, thread_sensitive=True)()
            result["services"]["db"] = "ok"
        except Exception as exc:
            result["services"]["db"] = f"error: {exc.__class__.__name__}"
            result["ok"] = False

        # Channels / Redis layer
        try:
            layer = get_channel_layer()
            if layer is None:
                result["services"]["channels"] = "disabled"
            else:
                await layer.send("healthz", {"type": "ping"})
                result["services"]["channels"] = "ok"
        except Exception as exc:
            result["services"]["channels"] = f"error: {exc.__class__.__name__}"
            result["ok"] = False

        # Counts (cheap signal that ORM is alive).
        try:
            result["counts"] = {
                "projects": await Project.objects.acount(),
                "issues": await Issue.objects.acount(),
            }
        except Exception:
            pass

        return HttpResponse(json.dumps(result), content_type="application/json")


class MarkdownPreviewView(AsyncLoginRequiredMixin, View):
    """Render markdown server-side and return sanitized HTML.

    Used by the split-pane editor to show a live preview without leaking
    the markdown renderer (and its bleach config) to the client.
    """

    async def post(self, request):
        from .markdown import render_markdown
        body = request.POST.get("body", "")[:50000]
        return HttpResponse(render_markdown(body))


class DashboardConfigView(AsyncLoginRequiredMixin, View):
    """List + persist the user's dashboard widget order/enabled set."""

    async def get(self, request):
        from accounts.models import DashboardWidget
        existing = {
            w.kind: w async for w in DashboardWidget.objects.filter(user=request.user)
        }
        widgets = []
        for kind, label in DashboardWidget.KIND_CHOICES:
            row = existing.get(kind)
            widgets.append({
                "kind": kind, "label": label,
                "enabled": row.enabled if row else True,
                "order": row.order if row else 99,
            })
        widgets.sort(key=lambda w: w["order"])
        return await arender(request, "core/dashboard_config.html", {"widgets": widgets})

    async def post(self, request):
        from accounts.models import DashboardWidget
        for i, kind in enumerate(request.POST.getlist("order")):
            enabled = bool(request.POST.get(f"enabled_{kind}"))
            await DashboardWidget.objects.aupdate_or_create(
                user=request.user, kind=kind,
                defaults={"order": i, "enabled": enabled},
            )
        return redirect("core:home")


class HomeView(AsyncLoginRequiredMixin, AsyncTemplateView):
    """Personal dashboard: assigned, watching, recently mentioned, pinned."""

    template_name = "core/home.html"

    async def aget_context_data(self, **kwargs):
        from accounts.models import DashboardWidget, Notification
        from issues.models import Pin, Visit
        ctx = await super().aget_context_data(**kwargs)
        user = self.request.user
        common = ("project", "status", "priority", "issue_type", "assignee")
        # Honor user's widget ordering/enabled-state. Anything not yet stored
        # falls back to default enabled with high order so it appears last.
        prefs = {
            w.kind: w async for w in DashboardWidget.objects.filter(user=user)
        }
        widget_order = []
        for kind, _label in DashboardWidget.KIND_CHOICES:
            row = prefs.get(kind)
            widget_order.append((kind, row.enabled if row else True, row.order if row else 99))
        widget_order.sort(key=lambda t: t[2])
        ctx["widget_order"] = [k for k, en, _ in widget_order if en]

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

        # "Mi día" — issues assigned to user, in an active sprint, ordered by
        # priority weight then due date. Top of dashboard for quick triage.
        from datetime import timedelta

        from django.utils import timezone as dj_tz

        soon = dj_tz.now().date() + timedelta(days=2)
        my_day_qs = (
            Issue.objects.filter(assignee=user)
            .exclude(status__category="done")
            .filter(Q(sprint__status="active") | Q(due_date__lte=soon))
            .select_related(*common).order_by("-priority__weight", "due_date")[:8]
        )
        ctx["my_day"] = [i async for i in my_day_qs]

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
