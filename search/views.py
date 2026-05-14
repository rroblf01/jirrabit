from django.http import HttpResponse
from django.shortcuts import redirect
from django.views import View

from core.aio import arender
from core.async_views import AsyncTemplateView
from core.mixins import AsyncLoginRequiredMixin
from issues.models import Issue
from projects.models import SavedFilter

from .jql import VALID_FIELDS, parse_jql


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
        suggestions: list[str] = []
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
                import difflib
                error = str(e)
                # When the parser complains about a field, surface the closest
                # known fields so the user can fix the typo without re-reading docs.
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
