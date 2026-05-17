from asgiref.sync import sync_to_async
from django.http import Http404
from django.shortcuts import redirect
from django.views import View

from core.aio import aform, arender
from core.async_views import (
    AsyncCreateView,
    AsyncDetailView,
    AsyncListView,
    AsyncTemplateView,
    AsyncUpdateView,
)
from core.mixins import AsyncLoginRequiredMixin
from core.permissions import aassert_can_admin

from .forms import EpicForm, ProjectForm, SprintForm
from .models import Project, Sprint


async def _aget_project(key):
    try:
        return await Project.objects.aget(key=key)
    except Project.DoesNotExist as exc:
        raise Http404(f"Project '{key}' not found") from exc


async def _aget_sprint(pk):
    try:
        return await Sprint.objects.select_related("project").aget(pk=pk)
    except Sprint.DoesNotExist as exc:
        raise Http404(f"Sprint {pk} not found") from exc


class ProjectListView(AsyncLoginRequiredMixin, AsyncListView):
    template_name = "projects/list.html"
    context_object_name = "projects"

    async def aget_queryset(self):
        return Project.objects.filter_visible(self.request.user)


class ProjectCreateView(AsyncLoginRequiredMixin, AsyncCreateView):
    form_class = ProjectForm
    template_name = "projects/form.html"

    def get_initial(self):
        return {"lead": self.request.user}

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        ctx["title"] = "Crear proyecto"
        return ctx

    async def aform_valid(self, form):
        project = form.save(commit=False)
        await project.asave()
        await project.members.aset(form.cleaned_data.get("members", []))
        self.object = project
        return redirect(project.get_absolute_url())


class ProjectDetailView(AsyncLoginRequiredMixin, AsyncDetailView):
    model = Project
    template_name = "projects/detail.html"
    slug_field = "key"
    slug_url_kwarg = "key"
    context_object_name = "project"

    async def aget_object(self):
        from core.permissions import aassert_can_view
        project = await _aget_project(self.kwargs["key"])
        await aassert_can_view(self.request.user, project)
        return project

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        project = self.object
        ctx["epics"] = [e async for e in project.epics.all()]
        ctx["sprints"] = [s async for s in project.sprints.all()]
        members = [m async for m in project.members.defer("avatar").all()]
        project._prefetched_objects_cache = {"members": members}
        return ctx


class ProjectUpdateView(AsyncLoginRequiredMixin, AsyncUpdateView):
    model = Project
    form_class = ProjectForm
    template_name = "projects/form.html"
    slug_field = "key"
    slug_url_kwarg = "key"

    async def aget_object(self):
        return await _aget_project(self.kwargs["key"])

    async def aget_form(self, form_class=None):
        return await aform(
            ProjectForm,
            self.request.POST or None,
            instance=self.object,
        )

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        ctx["title"] = f"Editar {self.object.key}"
        return ctx

    async def aform_valid(self, form):
        project = form.save(commit=False)
        await project.asave()
        await project.members.aset(form.cleaned_data.get("members", []))
        self.object = project
        return redirect(project.get_absolute_url())


class _ScopedToProjectMixin:
    """Resolve ``self.project`` from the ``key`` URL kwarg."""

    async def aget_project(self):
        return await _aget_project(self.kwargs["key"])


class EpicCreateView(AsyncLoginRequiredMixin, _ScopedToProjectMixin, AsyncCreateView):
    form_class = EpicForm
    template_name = "projects/epic_form.html"

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        ctx["project"] = self.project
        return ctx

    async def get(self, request, *args, **kwargs):
        from core.permissions import aassert_can_edit
        self.project = await self.aget_project()
        await aassert_can_edit(request.user, self.project)
        return await super().get(request, *args, **kwargs)

    async def post(self, request, *args, **kwargs):
        from core.permissions import aassert_can_edit
        self.project = await self.aget_project()
        await aassert_can_edit(request.user, self.project)
        return await super().post(request, *args, **kwargs)

    async def aform_valid(self, form):
        epic = form.save(commit=False)
        epic.project = self.project
        epic.created_by = self.request.user
        await epic.asave()
        self.object = epic
        if self.request.htmx:
            return await arender(self.request, "projects/_epic_row.html", {"epic": epic})
        return redirect(self.project.get_absolute_url())


class SprintCreateView(AsyncLoginRequiredMixin, _ScopedToProjectMixin, AsyncCreateView):
    form_class = SprintForm
    template_name = "projects/sprint_form.html"

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        ctx["project"] = self.project
        return ctx

    async def get(self, request, *args, **kwargs):
        from core.permissions import aassert_can_edit
        self.project = await self.aget_project()
        await aassert_can_edit(request.user, self.project)
        return await super().get(request, *args, **kwargs)

    async def post(self, request, *args, **kwargs):
        from core.permissions import aassert_can_edit
        self.project = await self.aget_project()
        await aassert_can_edit(request.user, self.project)
        return await super().post(request, *args, **kwargs)

    async def aform_valid(self, form):
        sprint = form.save(commit=False)
        sprint.project = self.project
        await sprint.asave()
        self.object = sprint
        if self.request.htmx:
            return await arender(self.request, "projects/_sprint_row.html", {"sprint": sprint})
        return redirect(self.project.get_absolute_url())


class SprintStartView(AsyncLoginRequiredMixin, View):
    async def post(self, request, sprint_id):
        sprint = await _aget_sprint(sprint_id)
        await aassert_can_admin(request.user, sprint.project)
        await sprint.astart()
        return await arender(request, "projects/_sprint_row.html", {"sprint": sprint})


class SprintCloseView(AsyncLoginRequiredMixin, View):
    async def post(self, request, sprint_id):
        sprint = await _aget_sprint(sprint_id)
        await aassert_can_admin(request.user, sprint.project)
        retro = request.POST.get("retro_notes", "").strip()
        if retro:
            sprint.retro_notes = retro
            await sprint.asave(update_fields=["retro_notes"])
        carry_id = request.POST.get("carry_to", "").strip()
        carry = None
        if carry_id:
            carry = await Sprint.objects.filter(
                pk=carry_id, project=sprint.project,
            ).exclude(status="closed").afirst()
        await sprint.aclose(carry_to=carry)
        return await arender(request, "projects/_sprint_row.html", {"sprint": sprint})


# --- activity feed, burndown, custom fields, webhooks UI, members admin ---

class SprintPlanningView(AsyncLoginRequiredMixin, AsyncTemplateView):
    """Capacity-aware sprint planning view.

    Computes per-assignee story-point load for the targeted sprint vs a
    configurable capacity. Capacity comes from ``?capacity=<sp>`` query
    string (per-user default). Useful to balance load before sprint start.
    """

    template_name = "projects/sprint_planning.html"

    async def aget_context_data(self, **kwargs):
        from collections import defaultdict

        from core.permissions import aassert_can_view
        from issues.models import Issue
        ctx = await super().aget_context_data(**kwargs)
        project = await _aget_project(self.kwargs["key"])
        await aassert_can_view(self.request.user, project)
        sprint_id = self.request.GET.get("sprint")
        sprint = None
        if sprint_id:
            sprint = await project.sprints.filter(pk=sprint_id).afirst()
        if sprint is None:
            sprint = await project.sprints.filter(status="active").afirst() \
                or await project.sprints.exclude(status="closed").order_by("start_date").afirst()
        try:
            capacity = int(self.request.GET.get("capacity", "20"))
        except ValueError:
            capacity = 20

        sp_by_user: dict = defaultdict(int)
        unassigned_sp = 0
        issues = []
        if sprint:
            issues = [
                i async for i in
                Issue.objects.filter(project=project, sprint=sprint)
                .select_related("assignee", "status", "priority", "issue_type")
            ]
            for i in issues:
                sp = i.story_points or 0
                if i.assignee_id:
                    sp_by_user[i.assignee_id] += sp
                else:
                    unassigned_sp += sp

        members = [m async for m in project.members.defer("avatar").all()]
        if project.lead_id and project.lead_id not in {m.pk for m in members}:
            members.insert(0, await project.members.through.objects.none().afirst() or project.lead)
        rows = []
        for m in members:
            load = sp_by_user.get(m.pk, 0)
            rows.append({
                "user": m,
                "load": load,
                "capacity": capacity,
                "pct": min(100, int(round(100 * load / capacity))) if capacity else 0,
                "over": load > capacity,
            })

        ctx["project"] = project
        ctx["sprint"] = sprint
        ctx["sprints"] = [s async for s in project.sprints.exclude(status="closed").order_by("start_date")]
        ctx["rows"] = rows
        ctx["unassigned_sp"] = unassigned_sp
        ctx["capacity"] = capacity
        ctx["issues"] = issues
        ctx["total_sp"] = sum((i.story_points or 0) for i in issues)
        return ctx


class ProjectDependencyGraphView(AsyncLoginRequiredMixin, AsyncTemplateView):
    """SVG-based dependency graph of issue links (blocks / blocked_by)."""

    template_name = "projects/dependency_graph.html"

    async def aget_context_data(self, **kwargs):
        from core.permissions import aassert_can_view
        from issues.models import Issue, IssueLink
        ctx = await super().aget_context_data(**kwargs)
        project = await _aget_project(self.kwargs["key"])
        await aassert_can_view(self.request.user, project)

        # Only "blocks" edges are interesting (others are inverses / lateral).
        links = [
            (link.source.key, link.target.key) async for link in
            IssueLink.objects.filter(source__project=project, type="blocks")
            .select_related("source", "target")
        ]
        # Collect nodes referenced by any block edge.
        referenced = set()
        for s, t in links:
            referenced.add(s)
            referenced.add(t)
        nodes = []
        if referenced:
            async for i in (
                Issue.objects.filter(project=project, key__in=referenced)
                .select_related("status", "priority")
            ):
                nodes.append({
                    "key": i.key, "summary": i.summary,
                    "status": str(i.status), "category": i.status.category,
                })

        # Layout: simple level-by-level BFS from "sources" (no incoming edges).
        in_deg = {n["key"]: 0 for n in nodes}
        for _s, t in links:
            in_deg[t] = in_deg.get(t, 0) + 1
        levels: dict[str, int] = {}
        frontier = [k for k, d in in_deg.items() if d == 0]
        adj: dict[str, list[str]] = {}
        for s, t in links:
            adj.setdefault(s, []).append(t)
        level = 0
        while frontier:
            for k in frontier:
                levels.setdefault(k, level)
            nxt = []
            for k in frontier:
                for t in adj.get(k, []):
                    if t not in levels:
                        nxt.append(t)
            frontier = nxt
            level += 1
        # Remaining nodes (cycles) go to level 0.
        for n in nodes:
            levels.setdefault(n["key"], 0)

        cols: dict[int, list] = {}
        for n in nodes:
            n["level"] = levels[n["key"]]
            cols.setdefault(n["level"], []).append(n)
        # Assign x/y for SVG: col = level, row = index in column.
        COL_W, ROW_H, NODE_W, NODE_H = 220, 60, 180, 36
        positions = {}
        for level_num, items in cols.items():
            for idx, n in enumerate(items):
                n["x"] = 20 + level_num * COL_W
                n["y"] = 20 + idx * ROW_H
                positions[n["key"]] = (n["x"], n["y"])

        edges = []
        for s, t in links:
            if s in positions and t in positions:
                sx, sy = positions[s]
                tx, ty = positions[t]
                edges.append({"x1": sx + NODE_W, "y1": sy + NODE_H / 2, "x2": tx, "y2": ty + NODE_H / 2})
        ctx["project"] = project
        ctx["nodes"] = nodes
        ctx["edges"] = edges
        ctx["node_w"] = NODE_W
        ctx["node_h"] = NODE_H
        max_x = max((n["x"] + NODE_W for n in nodes), default=400)
        max_y = max((n["y"] + NODE_H for n in nodes), default=200)
        ctx["svg_w"] = max(max_x + 20, 400)
        ctx["svg_h"] = max(max_y + 20, 200)
        return ctx


class ProjectRoadmapView(AsyncLoginRequiredMixin, AsyncTemplateView):
    """Gantt-like roadmap: epics laid out across project's date range."""

    template_name = "projects/roadmap.html"

    async def aget_context_data(self, **kwargs):
        from django.utils import timezone

        from core.permissions import aassert_can_view
        ctx = await super().aget_context_data(**kwargs)
        project = await _aget_project(self.kwargs["key"])
        await aassert_can_view(self.request.user, project)
        epics = [e async for e in project.epics.all().order_by("created_at")]

        # Range = earliest issue created → latest due/resolved or +60d.
        bounds = await sync_to_async(_compute_roadmap_range)(project)
        start, end = bounds
        total_days = max((end - start).days, 14)

        bars = []
        for e in epics:
            agg = await sync_to_async(_epic_bounds)(e)
            e_start, e_end, count, done = agg
            if e_start is None:
                continue
            left_pct = ((e_start - start).days / total_days) * 100
            width_pct = max(2.0, ((e_end - e_start).days / total_days) * 100)
            pct_done = int(round(100 * done / count)) if count else 0
            bars.append({
                "epic": e, "left_pct": round(left_pct, 2),
                "width_pct": round(width_pct, 2),
                "pct_done": pct_done, "count": count, "done": done,
                "start": e_start, "end": e_end,
            })

        # Vertical line for today.
        today_pct = ((timezone.localdate() - start).days / total_days) * 100
        ctx["project"] = project
        ctx["bars"] = bars
        ctx["start"] = start
        ctx["end"] = end
        ctx["today_pct"] = round(max(0, min(today_pct, 100)), 2)
        return ctx


def _compute_roadmap_range(project):
    from datetime import timedelta

    from django.db.models import Max, Min
    from django.utils import timezone

    from issues.models import Issue
    qs = Issue.objects.filter(project=project)
    agg = qs.aggregate(
        min_c=Min("created_at"), max_d=Max("due_date"), max_r=Max("resolved_at"),
    )
    today = timezone.localdate()
    start = (agg["min_c"].date() if agg["min_c"] else today)
    end = max(filter(None, [
        agg["max_d"], agg["max_r"].date() if agg["max_r"] else None, today + timedelta(days=30),
    ]))
    if end <= start:
        end = start + timedelta(days=30)
    return start, end


def _epic_bounds(epic):
    from django.db.models import Max, Min
    qs = epic.issues.all()
    count = qs.count()
    done = qs.filter(status__category="done").count()
    agg = qs.aggregate(start=Min("created_at"), end_due=Max("due_date"), end_res=Max("resolved_at"))
    start_dt = agg["start"]
    end_due = agg["end_due"]
    end_res = agg["end_res"].date() if agg["end_res"] else None
    if not start_dt:
        return None, None, count, done
    end = max(filter(None, [end_due, end_res, start_dt.date()]))
    return start_dt.date(), end, count, done


class ProjectSlaView(AsyncLoginRequiredMixin, AsyncTemplateView):
    """Surface issues that have been stuck in a single status > threshold.

    SLA is computed from ``issues.HistoryEntry`` (field="status") rows: the
    timestamp of the most recent status change is the issue's "time at
    current status". Open issues that exceed the configured day threshold
    are flagged.
    """

    template_name = "projects/sla.html"

    async def aget_context_data(self, **kwargs):
        from datetime import timedelta

        from django.utils import timezone

        from core.permissions import aassert_can_view
        from issues.models import HistoryEntry, Issue
        ctx = await super().aget_context_data(**kwargs)
        project = await _aget_project(self.kwargs["key"])
        await aassert_can_view(self.request.user, project)
        try:
            threshold = max(1, int(self.request.GET.get("days", "7")))
        except ValueError:
            threshold = 7
        now = timezone.now()
        cutoff = now - timedelta(days=threshold)
        candidates = [
            i async for i in
            Issue.objects.filter(project=project, archived=False)
            .exclude(status__category="done")
            .select_related("status", "priority", "assignee")
            .order_by("updated_at")
        ]
        rows = []
        for i in candidates:
            last_change = await HistoryEntry.objects.filter(
                issue=i, field="status",
            ).order_by("-created_at").afirst()
            entered_at = last_change.created_at if last_change else i.created_at
            if entered_at <= cutoff:
                rows.append({
                    "issue": i,
                    "entered_at": entered_at,
                    "days_in_status": int((now - entered_at).total_seconds() / 86400),
                })
        ctx["project"] = project
        ctx["rows"] = sorted(rows, key=lambda r: -r["days_in_status"])
        ctx["threshold"] = threshold
        return ctx


class ProjectWikiView(AsyncLoginRequiredMixin, View):
    """Render & edit a project's wiki page.

    Reading is open to any project viewer; editing requires admin role.
    Body is markdown rendered with the same sanitiser used everywhere else.
    """

    async def get(self, request, key):
        from core.markdown import render_markdown
        from core.permissions import aassert_can_view, aget_role, can_admin

        from .models import ProjectWiki
        project = await _aget_project(key)
        await aassert_can_view(request.user, project)
        wiki = await ProjectWiki.objects.filter(project=project).afirst()
        role = await aget_role(request.user, project)
        edit_mode = request.GET.get("edit") == "1"
        return await arender(
            request, "projects/wiki.html",
            {
                "project": project,
                "wiki": wiki,
                "body_html": render_markdown(wiki.body) if wiki else "",
                "can_edit": can_admin(role) or request.user.is_superuser,
                "edit_mode": edit_mode,
            },
        )

    async def post(self, request, key):
        from core.permissions import aassert_can_admin

        from .models import ProjectWiki
        project = await _aget_project(key)
        await aassert_can_admin(request.user, project)
        body = request.POST.get("body", "")
        wiki, _ = await ProjectWiki.objects.aget_or_create(project=project)
        wiki.body = body
        wiki.updated_by = request.user
        await wiki.asave()
        return redirect("projects:wiki", key=project.key)


class WorkloadHeatmapView(AsyncLoginRequiredMixin, AsyncTemplateView):
    """Calendar grid showing daily SP load per project member.

    For each user in the project, plot a row of cells (one per day, last 30
    days). Cell color intensity = sum of story_points of issues whose
    ``resolved_at`` falls on that day OR ``due_date`` if still open.
    """

    template_name = "projects/heatmap.html"

    async def aget_context_data(self, **kwargs):
        from collections import defaultdict
        from datetime import timedelta

        from django.utils import timezone

        from core.permissions import aassert_can_view
        from issues.models import Issue
        ctx = await super().aget_context_data(**kwargs)
        project = await _aget_project(self.kwargs["key"])
        await aassert_can_view(self.request.user, project)
        today = timezone.localdate()
        try:
            days_back = max(7, min(int(self.request.GET.get("days", "30")), 90))
        except ValueError:
            days_back = 30
        start = today - timedelta(days=days_back - 1)
        days = [start + timedelta(days=i) for i in range(days_back)]

        members = [m async for m in project.members.defer("avatar").order_by("username")]
        load: dict[int, dict] = defaultdict(lambda: defaultdict(int))
        async for i in (
            Issue.objects.filter(project=project, assignee__isnull=False).only(
                "assignee_id", "story_points", "resolved_at", "due_date",
            )
        ):
            sp = i.story_points or 0
            if not sp:
                continue
            day = None
            if i.resolved_at and start <= i.resolved_at.date() <= today:
                day = i.resolved_at.date()
            elif i.due_date and start <= i.due_date <= today + timedelta(days=days_back):
                day = i.due_date
            if day and start <= day <= today + timedelta(days=days_back):
                if day < start:
                    continue
                load[i.assignee_id][day] += sp

        max_load = max(
            (n for u in load.values() for n in u.values()), default=0,
        ) or 1
        rows = []
        for m in members:
            cells = []
            for d in days:
                v = load.get(m.pk, {}).get(d, 0)
                intensity = int(round(100 * v / max_load))
                cells.append({"date": d, "sp": v, "intensity": intensity})
            rows.append({"user": m, "cells": cells, "total": sum(c["sp"] for c in cells)})
        ctx["project"] = project
        ctx["rows"] = rows
        ctx["days"] = days
        ctx["max_load"] = max_load
        ctx["days_back"] = days_back
        return ctx


class ProjectReportsView(AsyncLoginRequiredMixin, AsyncTemplateView):
    """Throughput, cycle time, and WIP-by-status snapshot."""

    template_name = "projects/reports.html"

    async def aget_context_data(self, **kwargs):
        from datetime import timedelta

        from django.utils import timezone

        from issues.models import HistoryEntry, Issue, Status

        ctx = await super().aget_context_data(**kwargs)
        project = await _aget_project(self.kwargs["key"])
        from core.permissions import aassert_can_view
        await aassert_can_view(self.request.user, project)
        ctx["project"] = project

        # --- Throughput: issues resolved per ISO-week, last 8 weeks.
        now = timezone.now()
        weeks_back = 8
        week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        first_week = week_start - timedelta(weeks=weeks_back - 1)
        resolved = [
            i async for i in
            Issue.objects.filter(
                project=project, resolved_at__gte=first_week,
            ).only("resolved_at")
        ]
        buckets = {first_week + timedelta(weeks=w): 0 for w in range(weeks_back)}
        for i in resolved:
            b = (i.resolved_at - timedelta(days=i.resolved_at.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
            if b in buckets:
                buckets[b] += 1
        throughput = [
            {"week": b.strftime("%Y-W%V"), "count": c}
            for b, c in sorted(buckets.items())
        ]
        ctx["throughput"] = throughput
        ctx["throughput_max"] = max((t["count"] for t in throughput), default=0) or 1

        # --- Cycle time: hours between first in_progress-category transition
        # and resolved_at, for issues resolved in the last 90 days.
        cycle_since = now - timedelta(days=90)
        recent_resolved = [
            i async for i in
            Issue.objects.filter(
                project=project, resolved_at__gte=cycle_since,
            ).only("id", "key", "resolved_at", "created_at")
        ]
        recent_ids = [i.pk for i in recent_resolved]
        starts = {}
        if recent_ids:
            async for h in HistoryEntry.objects.filter(
                issue_id__in=recent_ids, field="status",
            ).order_by("issue_id", "created_at").only(
                "issue_id", "new_value", "created_at",
            ):
                if h.issue_id in starts:
                    continue
                starts[h.issue_id] = h.created_at
        durations = []
        for i in recent_resolved:
            start = starts.get(i.pk, i.created_at)
            if i.resolved_at and start and i.resolved_at > start:
                durations.append((i.resolved_at - start).total_seconds() / 3600.0)
        if durations:
            durations.sort()
            mid = len(durations) // 2
            median_h = (
                durations[mid] if len(durations) % 2
                else (durations[mid - 1] + durations[mid]) / 2
            )
            avg_h = sum(durations) / len(durations)
            p90_h = durations[int(0.9 * (len(durations) - 1))]
        else:
            median_h = avg_h = p90_h = 0
        ctx["cycle"] = {
            "count": len(durations),
            "median_h": round(median_h, 1),
            "avg_h": round(avg_h, 1),
            "p90_h": round(p90_h, 1),
        }

        # --- WIP: open issue count per status (snapshot).
        statuses = [s async for s in Status.objects.order_by("order").all()]
        wip = []
        for s in statuses:
            n = await Issue.objects.filter(project=project, status=s).acount()
            wip.append({"name": s.name, "category": s.category, "count": n})
        ctx["wip"] = wip
        ctx["wip_max"] = max((w["count"] for w in wip), default=0) or 1
        return ctx


class ProjectActivityView(AsyncLoginRequiredMixin, AsyncTemplateView):
    template_name = "projects/activity.html"
    PAGE_SIZE = 50

    async def aget_context_data(self, **kwargs):
        from issues.models import AuditEntry, HistoryEntry
        ctx = await super().aget_context_data(**kwargs)
        project = await _aget_project(self.kwargs["key"])
        try:
            page = max(int(self.request.GET.get("page", "1")), 1)
        except ValueError:
            page = 1
        offset = (page - 1) * self.PAGE_SIZE
        end = offset + self.PAGE_SIZE + 1
        audits = [
            a async for a in
            AuditEntry.objects.filter(project=project)
            .select_related("actor")[offset:end]
        ]
        history = [
            h async for h in
            HistoryEntry.objects.filter(issue__project=project)
            .select_related("actor", "issue")[offset:end]
        ]
        ctx["project"] = project
        ctx["audits"] = audits[: self.PAGE_SIZE]
        ctx["history"] = history[: self.PAGE_SIZE]
        ctx["page"] = page
        ctx["next_page"] = page + 1 if (len(audits) > self.PAGE_SIZE or len(history) > self.PAGE_SIZE) else None
        ctx["prev_page"] = page - 1 if page > 1 else None
        return ctx


class ProjectBurndownView(AsyncLoginRequiredMixin, AsyncTemplateView):
    template_name = "projects/burndown.html"

    async def aget_context_data(self, **kwargs):
        from datetime import timedelta

        from django.utils import timezone
        ctx = await super().aget_context_data(**kwargs)
        project = await _aget_project(self.kwargs["key"])
        sprint_id = self.request.GET.get("sprint")
        sprint = None
        if sprint_id:
            sprint = await project.sprints.filter(pk=sprint_id).afirst()
        if sprint is None:
            sprint = await project.sprints.filter(status="active").afirst() or await project.sprints.order_by("-start_date").afirst()
        ctx["project"] = project
        ctx["sprint"] = sprint
        ctx["sprints"] = [s async for s in project.sprints.all()]

        # Velocity: closed sprints, committed (sum SP at start) vs
        # completed (sum SP of issues resolved during the sprint).
        # Single query for all sprint issues, grouped in Python.
        from issues.models import Issue
        closed_sprints = [
            s async for s in
            project.sprints.filter(status="closed").order_by("end_date")[:12]
        ]
        closed_ids = [s.pk for s in closed_sprints]
        issues_by_sprint: dict[int, list] = {sid: [] for sid in closed_ids}
        if closed_ids:
            async for i in Issue.objects.filter(sprint_id__in=closed_ids).only(
                "sprint_id", "story_points", "resolved_at",
            ):
                issues_by_sprint[i.sprint_id].append(i)
        velocity = []
        for s in closed_sprints:
            sprint_issues = issues_by_sprint.get(s.pk, [])
            committed = sum(i.story_points or 0 for i in sprint_issues)
            completed = sum(
                (i.story_points or 0) for i in sprint_issues
                if i.resolved_at and s.start_date and s.end_date
                and s.start_date <= i.resolved_at.date() <= s.end_date
            )
            velocity.append({"name": s.name, "committed": committed, "completed": completed})

        # Pre-compute SVG coordinates so the template stays free of
        # arithmetic. ``CHART_HEIGHT`` and ``BAR_WIDTH`` match the values
        # in ``burndown.html``; ``GROUP_WIDTH`` spaces the sprint groups.
        CHART_HEIGHT = 200
        BAR_WIDTH = 20
        GROUP_WIDTH = 60
        v_max = max((max(v["committed"], v["completed"]) for v in velocity), default=0) or 1
        for idx, v in enumerate(velocity):
            v["x"] = idx * GROUP_WIDTH + 4
            v["h_committed"] = round(v["committed"] * CHART_HEIGHT / v_max)
            v["h_completed"] = round(v["completed"] * CHART_HEIGHT / v_max)
            v["y_committed"] = CHART_HEIGHT - v["h_committed"]
            v["y_completed"] = CHART_HEIGHT - v["h_completed"]
            v["bar_width"] = BAR_WIDTH
            v["label_x"] = v["x"] + BAR_WIDTH
        ctx["velocity"] = velocity
        ctx["velocity_max"] = v_max
        ctx["velocity_chart_height"] = CHART_HEIGHT

        if sprint and sprint.start_date and sprint.end_date:
            from issues.models import Issue
            issues = [
                i async for i in
                Issue.objects.filter(sprint=sprint).only("story_points", "resolved_at")
            ]
            total_sp = sum(i.story_points or 0 for i in issues)
            days = (sprint.end_date - sprint.start_date).days or 1
            today = timezone.localdate()
            points = []
            for i in range(days + 1):
                d = sprint.start_date + timedelta(days=i)
                remaining = sum(
                    (i.story_points or 0) for i in issues
                    if not (i.resolved_at and i.resolved_at.date() <= d)
                )
                actual = remaining if d <= today else None
                ideal = total_sp - (total_sp / days) * i
                points.append({"date": d.isoformat(), "ideal": round(ideal, 1), "actual": actual})
            ctx["total_sp"] = total_sp
            ctx["points"] = points
            done_sp = sum(i.story_points or 0 for i in issues if i.resolved_at)
            ctx["done_sp"] = done_sp
            ctx["percent_done"] = round(done_sp * 100.0 / total_sp, 1) if total_sp else 0
        return ctx


# --- members admin (project role management) ---

class ProjectMembersView(AsyncLoginRequiredMixin, AsyncTemplateView):
    template_name = "projects/members.html"

    async def aget_context_data(self, **kwargs):
        from accounts.models import User
        ctx = await super().aget_context_data(**kwargs)
        project = await _aget_project(self.kwargs["key"])
        await aassert_can_admin(self.request.user, project)
        ctx["project"] = project
        ctx["memberships"] = [
            m async for m in
            project.memberships.select_related("user").defer("user__avatar")
        ]
        member_ids = {m.user_id for m in ctx["memberships"]}
        ctx["available_users"] = [
            u async for u in
            User.objects.filter(is_active=True)
            .defer("avatar")
            .exclude(pk__in=member_ids).order_by("username")
        ]
        return ctx


class ProjectMembershipAddView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key):
        from django.http import HttpResponseBadRequest

        from accounts.models import User
        from projects.models import ProjectMembership
        project = await _aget_project(key)
        await aassert_can_admin(request.user, project)
        try:
            user_id = int(request.POST.get("user_id", ""))
        except (TypeError, ValueError):
            return HttpResponseBadRequest("user_id inválido")
        role = request.POST.get("role", "member")
        if role not in {"admin", "member", "viewer"}:
            return HttpResponseBadRequest("rol inválido")
        user = await User.objects.filter(pk=user_id, is_active=True).afirst()
        if user is None:
            return HttpResponseBadRequest("usuario no encontrado")
        await ProjectMembership.objects.aget_or_create(
            project=project, user=user, defaults={"role": role},
        )
        return redirect("projects:members", key=project.key)


class ProjectMembershipUpdateView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key, pk):
        from django.http import Http404

        from projects.models import ProjectMembership
        project = await _aget_project(key)
        await aassert_can_admin(request.user, project)
        m = await ProjectMembership.objects.filter(pk=pk, project=project).afirst()
        if m is None:
            raise Http404("Membership not found in this project")
        action = request.POST.get("action")
        if action == "delete":
            await m.adelete()
        else:
            new_role = request.POST.get("role", m.role)
            if new_role not in {"admin", "member", "viewer"}:
                from django.http import HttpResponseBadRequest
                return HttpResponseBadRequest("rol inválido")
            m.role = new_role
            await m.asave(update_fields=["role"])
        return redirect("projects:members", key=project.key)


# --- custom fields admin ---

class ProjectCustomFieldsView(AsyncLoginRequiredMixin, AsyncTemplateView):
    template_name = "projects/custom_fields.html"

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        project = await _aget_project(self.kwargs["key"])
        await aassert_can_admin(self.request.user, project)
        ctx["project"] = project
        ctx["fields"] = [f async for f in project.custom_fields.all()]
        return ctx


class ProjectCustomFieldCreateView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key):
        from django.utils.text import slugify

        from projects.models import CustomFieldDef
        project = await _aget_project(key)
        await aassert_can_admin(request.user, project)
        name = request.POST.get("name", "").strip()
        if not name:
            return redirect("projects:custom_fields", key=project.key)
        await CustomFieldDef.objects.acreate(
            project=project,
            name=name,
            slug=slugify(name)[:80],
            type=request.POST.get("type", "text"),
            required=bool(request.POST.get("required")),
            options=request.POST.get("options", ""),
        )
        return redirect("projects:custom_fields", key=project.key)


class ProjectCustomFieldDeleteView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key, pk):
        from projects.models import CustomFieldDef
        project = await _aget_project(key)
        await aassert_can_admin(request.user, project)
        await CustomFieldDef.objects.filter(pk=pk, project=project).adelete()
        return redirect("projects:custom_fields", key=project.key)


# --- webhooks ---

class ProjectWebhooksView(AsyncLoginRequiredMixin, AsyncTemplateView):
    template_name = "projects/webhooks.html"

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        project = await _aget_project(self.kwargs["key"])
        await aassert_can_admin(self.request.user, project)
        ctx["project"] = project
        ctx["webhooks"] = [w async for w in project.webhooks.all()]
        return ctx


class ProjectWebhookCreateView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key):
        from projects.models import Webhook
        project = await _aget_project(key)
        await aassert_can_admin(request.user, project)
        await Webhook.objects.acreate(
            project=project,
            name=request.POST.get("name", "").strip() or "webhook",
            url=request.POST.get("url", "").strip(),
            secret=request.POST.get("secret", "").strip(),
            events=request.POST.get("events", "issue.created,issue.updated"),
        )
        return redirect("projects:webhooks", key=project.key)


class ProjectWebhookDeleteView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key, pk):
        from projects.models import Webhook
        project = await _aget_project(key)
        await aassert_can_admin(request.user, project)
        await Webhook.objects.filter(pk=pk, project=project).adelete()
        return redirect("projects:webhooks", key=project.key)
