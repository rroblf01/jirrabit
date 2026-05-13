from django.http import Http404
from django.shortcuts import redirect
from django.views import View

from core.aio import aform, arender
from core.async_views import (
    AsyncCreateView,
    AsyncDetailView,
    AsyncListView,
    AsyncUpdateView,
)
from core.hooks import dispatch
from core.mixins import AsyncLoginRequiredMixin

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
        dispatch("project.created", project=project, actor=self.request.user)
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
        dispatch("project.updated", project=project, actor=self.request.user)
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
        dispatch("epic.created", epic=epic, project=self.project, actor=self.request.user)
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
        dispatch("sprint.created", sprint=sprint, project=self.project, actor=self.request.user)
        if self.request.htmx:
            return await arender(self.request, "projects/_sprint_row.html", {"sprint": sprint})
        return redirect(self.project.get_absolute_url())


class SprintStartView(AsyncLoginRequiredMixin, View):
    async def post(self, request, sprint_id):
        sprint = await _aget_sprint(sprint_id)
        await sprint.astart()
        dispatch("sprint.started", sprint=sprint, actor=request.user)
        return await arender(request, "projects/_sprint_row.html", {"sprint": sprint})


class SprintCloseView(AsyncLoginRequiredMixin, View):
    async def post(self, request, sprint_id):
        sprint = await _aget_sprint(sprint_id)
        await sprint.aclose()
        dispatch("sprint.closed", sprint=sprint, actor=request.user)
        return await arender(request, "projects/_sprint_row.html", {"sprint": sprint})
