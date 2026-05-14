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
        return await Sprint.objects.aget(pk=pk)
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
        return await _aget_project(self.kwargs["key"])

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        project = self.object
        ctx["epics"] = [e async for e in project.epics.all()]
        ctx["sprints"] = [s async for s in project.sprints.all()]
        members = [m async for m in project.members.all()]
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
        self.project = await self.aget_project()
        return await super().get(request, *args, **kwargs)

    async def post(self, request, *args, **kwargs):
        self.project = await self.aget_project()
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
        self.project = await self.aget_project()
        return await super().get(request, *args, **kwargs)

    async def post(self, request, *args, **kwargs):
        self.project = await self.aget_project()
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
        await sprint.astart()
        return await arender(request, "projects/_sprint_row.html", {"sprint": sprint})


class SprintCloseView(AsyncLoginRequiredMixin, View):
    async def post(self, request, sprint_id):
        sprint = await _aget_sprint(sprint_id)
        await sprint.aclose()
        return await arender(request, "projects/_sprint_row.html", {"sprint": sprint})


# --- activity feed, burndown, custom fields, webhooks UI, members admin ---

class ProjectActivityView(AsyncLoginRequiredMixin, AsyncTemplateView):
    template_name = "projects/activity.html"

    async def aget_context_data(self, **kwargs):
        from issues.models import AuditEntry, HistoryEntry
        ctx = await super().aget_context_data(**kwargs)
        project = await _aget_project(self.kwargs["key"])
        ctx["project"] = project
        ctx["audits"] = [a async for a in AuditEntry.objects.filter(project=project).select_related("actor")[:100]]
        ctx["history"] = [h async for h in HistoryEntry.objects.filter(issue__project=project).select_related("actor", "issue")[:100]]
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
        from issues.models import Issue
        velocity = []
        async for s in project.sprints.filter(status="closed").order_by("end_date")[:12]:
            sprint_issues = [i async for i in Issue.objects.filter(sprint=s)]
            committed = sum(i.story_points or 0 for i in sprint_issues)
            completed = sum(
                (i.story_points or 0) for i in sprint_issues
                if i.resolved_at and s.start_date and s.end_date
                and s.start_date <= i.resolved_at.date() <= s.end_date
            )
            velocity.append({"name": s.name, "committed": committed, "completed": completed})
        ctx["velocity"] = velocity
        ctx["velocity_max"] = max(
            (max(v["committed"], v["completed"]) for v in velocity), default=0
        ) or 1

        if sprint and sprint.start_date and sprint.end_date:
            from issues.models import Issue
            issues = [i async for i in Issue.objects.filter(sprint=sprint)]
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
        from projects.models import ProjectMembership
        from accounts.models import User
        ctx = await super().aget_context_data(**kwargs)
        project = await _aget_project(self.kwargs["key"])
        await aassert_can_admin(self.request.user, project)
        ctx["project"] = project
        ctx["memberships"] = [m async for m in project.memberships.select_related("user").all()]
        member_ids = {m.user_id for m in ctx["memberships"]}
        ctx["available_users"] = [
            u async for u in User.objects.filter(is_active=True).exclude(pk__in=member_ids).order_by("username")
        ]
        return ctx


class ProjectMembershipAddView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key):
        from projects.models import ProjectMembership
        from accounts.models import User
        project = await _aget_project(key)
        await aassert_can_admin(request.user, project)
        user_id = request.POST.get("user_id")
        role = request.POST.get("role", "member")
        user = await User.objects.aget(pk=user_id)
        await ProjectMembership.objects.acreate(project=project, user=user, role=role)
        return redirect("projects:members", key=project.key)


class ProjectMembershipUpdateView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key, pk):
        from projects.models import ProjectMembership
        project = await _aget_project(key)
        await aassert_can_admin(request.user, project)
        m = await ProjectMembership.objects.aget(pk=pk, project=project)
        action = request.POST.get("action")
        if action == "delete":
            await m.adelete()
        else:
            m.role = request.POST.get("role", m.role)
            await m.asave()
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
