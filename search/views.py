from django.http import HttpResponse
from django.shortcuts import redirect
from django.views import View

from core.aio import arender
from core.async_views import AsyncTemplateView
from core.mixins import AsyncLoginRequiredMixin
from issues.models import Issue
from projects.models import SavedFilter

from .jql import VALID_FIELDS, parse_jql


class SearchSuggestView(AsyncLoginRequiredMixin, View):
    """Lightweight typeahead: top 5 issues by key/summary match, scoped to visible projects."""

    async def get(self, request):
        from django.db.models import Q

        from projects.models import Project
        q = request.GET.get("q", "").strip()
        if len(q) < 2:
            return await arender(request, "search/_typeahead.html", {"items": [], "q": q})
        visible = Project.objects.filter_visible(request.user)
        qs = (
            Issue.objects.filter(project__in=visible)
            .filter(Q(key__icontains=q) | Q(summary__icontains=q))
            .select_related("status", "project")
            .order_by("-updated_at")[:6]
        )
        items = [i async for i in qs]
        return await arender(request, "search/_typeahead.html", {"items": items, "q": q})


class QuickSwitchView(AsyncLoginRequiredMixin, View):
    """JSON endpoint for the Ctrl/Cmd+K quick switcher.

    Returns a unified list of issues, projects and saved filters that match
    the query. Each entry has a ``type``, ``label`` and ``url``.
    """

    async def get(self, request):
        import json

        from django.db.models import Q
        from django.http import HttpResponse

        from projects.models import Project
        q = request.GET.get("q", "").strip()
        items: list[dict] = []
        if not q:
            return HttpResponse(json.dumps({"items": []}), content_type="application/json")
        visible = Project.objects.filter_visible(request.user)
        # Issues
        async for i in (
            Issue.objects.filter(project__in=visible)
            .filter(Q(key__icontains=q) | Q(summary__icontains=q))
            .select_related("status", "project")
            .order_by("-updated_at")[:8]
        ):
            items.append({
                "type": "issue",
                "label": f"{i.key} — {i.summary}",
                "hint": str(i.status),
                "url": i.get_absolute_url(),
            })
        # Projects
        async for p in (
            visible.filter(Q(key__icontains=q) | Q(name__icontains=q)).order_by("key")[:5]
        ):
            items.append({
                "type": "project",
                "label": f"{p.key} — {p.name}",
                "hint": "Proyecto",
                "url": p.get_absolute_url(),
            })
        # Saved filters
        async for f in (
            SavedFilter.objects.filter(
                Q(owner=request.user) | Q(scope="shared"),
            ).filter(name__icontains=q).order_by("name")[:5]
        ):
            items.append({
                "type": "filter",
                "label": f.name,
                "hint": "Saved filter",
                "url": f"/search/?q={f.query}",
            })
        return HttpResponse(json.dumps({"items": items}), content_type="application/json")


class SearchView(AsyncLoginRequiredMixin, AsyncTemplateView):
    def get_template_names(self):
        if self.request.htmx:
            return ["search/_results.html"]
        return ["search/search.html"]

    PAGE_SIZE = 50

    async def aget_context_data(self, **kwargs):
        from .jql import JQLError
        ctx = await super().aget_context_data(**kwargs)
        query = self.request.GET.get("q", "").strip()
        try:
            page = max(int(self.request.GET.get("page", "1")), 1)
        except ValueError:
            page = 1
        offset = (page - 1) * self.PAGE_SIZE
        issues: list = []
        error: str | None = None
        suggestions: list[str] = []
        has_more = False
        if query:
            try:
                q, order = parse_jql(query)
                qs = (
                    Issue.objects.filter(q)
                    .select_related("project", "status", "priority", "issue_type", "assignee")
                    .distinct()
                    .order_by(*(order or ["-updated_at"]))
                )
                rows = [
                    i async for i in qs[offset : offset + self.PAGE_SIZE + 1]
                ]
                has_more = len(rows) > self.PAGE_SIZE
                issues = rows[: self.PAGE_SIZE]
            except JQLError as e:
                import difflib
                error = str(e)
                if "Campo desconocido:" in error:
                    bad = error.split("'")[1] if "'" in error else ""
                    if bad:
                        suggestions = difflib.get_close_matches(
                            bad, list(VALID_FIELDS), n=3, cutoff=0.5
                        )
        ctx["query"] = query
        ctx["issues"] = issues
        ctx["error"] = error
        ctx["suggestions"] = suggestions
        ctx["page"] = page
        ctx["next_page"] = page + 1 if has_more else None
        ctx["prev_page"] = page - 1 if page > 1 else None
        ctx["valid_fields"] = sorted(VALID_FIELDS)
        from django.db.models import Q
        ctx["saved_filters"] = [
            f async for f in SavedFilter.objects.filter(
                Q(owner=self.request.user) | Q(scope="shared")
            ).order_by("name")
        ]
        return ctx


class SavedFilterCreateView(AsyncLoginRequiredMixin, View):
    async def post(self, request):
        name = request.POST.get("name", "").strip()
        query = request.POST.get("query", "").strip()
        scope = request.POST.get("scope", "private")
        if not name or not query:
            return HttpResponse(status=400)
        await SavedFilter.objects.acreate(
            owner=request.user, name=name, query=query,
            scope="shared" if scope == "shared" else "private",
        )
        return redirect(f"/search/?q={query}")


class SavedFilterDeleteView(AsyncLoginRequiredMixin, View):
    async def post(self, request, pk):
        qs = SavedFilter.objects.filter(pk=pk)
        if not request.user.is_superuser:
            qs = qs.filter(owner=request.user)
        f = await qs.afirst()
        if f is None:
            from django.core.exceptions import PermissionDenied
            from django.utils.translation import gettext as _
            raise PermissionDenied(_("No puedes borrar este filtro."))
        await f.adelete()
        if request.htmx:
            from django.http import HttpResponse
            return HttpResponse("")
        return redirect("/search/")
