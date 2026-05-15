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
        from projects.models import CustomFieldDef
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
        from projects.models import Webhook
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
