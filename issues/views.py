from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views import View

from core.aio import aform, arender, avalid
from core.async_views import (
    AsyncCreateView,
    AsyncDetailView,
    AsyncListView,
    AsyncUpdateView,
)
from core.mixins import AsyncLoginRequiredMixin
from projects.models import Project

from .forms import CommentForm, IssueForm
from .models import Attachment, Issue, IssueType, Priority, Status


async def _aget_project(key):
    try:
        return await Project.objects.aget(key=key)
    except Project.DoesNotExist as exc:
        raise Http404(f"Project '{key}' not found") from exc


async def _aget_issue(key):
    qs = Issue.objects.select_related(
        "project", "status", "priority", "issue_type", "assignee", "reporter"
    )
    try:
        return await qs.aget(key=key)
    except Issue.DoesNotExist as exc:
        raise Http404(f"Issue '{key}' not found") from exc


async def _aget_status(pk):
    try:
        return await Status.objects.aget(pk=pk)
    except Status.DoesNotExist as exc:
        raise Http404(f"Status {pk} not found") from exc


class IssueCreateView(AsyncLoginRequiredMixin, AsyncCreateView):
    form_class = IssueForm
    template_name = "issues/form.html"

    async def get(self, request, *args, **kwargs):
        self.project = await _aget_project(kwargs["key"])
        return await super().get(request, *args, **kwargs)

    async def post(self, request, *args, **kwargs):
        self.project = await _aget_project(kwargs["key"])
        return await super().post(request, *args, **kwargs)

    async def aget_form(self, form_class=None):
        if self.request.method == "POST":
            return IssueForm(self.request.POST, project=self.project)
        status = await Status.objects.order_by("order").afirst()
        priority = await Priority.objects.afirst()
        itype = await IssueType.objects.afirst()
        return IssueForm(
            project=self.project,
            initial={"status": status, "priority": priority, "issue_type": itype},
        )

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        ctx["project"] = self.project
        return ctx

    async def aform_valid(self, form):
        issue = form.save(commit=False)
        issue.project = self.project
        issue.reporter = self.request.user
        if not issue.priority_id:
            issue.priority = await Priority.objects.afirst()
        if not issue.status_id:
            issue.status = await Status.objects.order_by("order").afirst()
        num = await self.project.anext_issue_number()
        issue.key = f"{self.project.key}-{num}"
        await issue.asave()
        await issue.labels.aset(form.cleaned_data.get("labels", []))
        self.object = issue
        if self.request.htmx:
            return HttpResponse(
                status=204, headers={"HX-Redirect": issue.get_absolute_url()}
            )
        return redirect(issue.get_absolute_url())


class IssueDetailView(AsyncLoginRequiredMixin, AsyncDetailView):
    model = Issue
    template_name = "issues/detail.html"
    context_object_name = "issue"

    async def aget_object(self):
        return await _aget_issue(self.kwargs["key"])

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        ctx["comment_form"] = CommentForm()
        ctx["statuses"] = [s async for s in Status.objects.all()]
        ctx["priorities"] = [p async for p in Priority.objects.all()]
        ctx["is_watching"] = await self.object.watchers.filter(
            pk=self.request.user.pk
        ).aexists()
        return ctx


class IssueUpdateView(AsyncLoginRequiredMixin, AsyncUpdateView):
    model = Issue
    form_class = IssueForm
    template_name = "issues/form.html"

    async def aget_object(self):
        return await _aget_issue(self.kwargs["key"])

    async def aget_form(self, form_class=None):
        return await aform(
            IssueForm,
            self.request.POST or None,
            instance=self.object,
            project=self.object.project,
        )

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        ctx["project"] = self.object.project
        ctx["issue"] = self.object
        return ctx

    async def aform_valid(self, form):
        issue = form.save(commit=False)
        await issue.asave()
        await issue.labels.aset(form.cleaned_data.get("labels", []))
        self.object = issue
        return redirect(issue.get_absolute_url())


class IssueListView(AsyncLoginRequiredMixin, AsyncListView):
    template_name = "issues/list.html"
    context_object_name = "issues"

    async def aget_queryset(self):
        self.project = await _aget_project(self.kwargs["key"])
        return self.project.issues.select_related(
            "status", "priority", "assignee", "issue_type"
        ).all()

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        ctx["project"] = self.project
        return ctx


class ChangeStatusView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key):
        issue = await _aget_issue(key)
        status = await _aget_status(request.POST.get("status"))
        issue.status = status
        if status.category == "done" and not issue.resolved_at:
            issue.resolved_at = timezone.now()
        elif status.category != "done":
            issue.resolved_at = None
        await issue.asave()
        if request.htmx:
            statuses = [s async for s in Status.objects.all()]
            return await arender(
                request,
                "issues/_status_badge.html",
                {"issue": issue, "statuses": statuses},
            )
        return redirect(issue.get_absolute_url())


class AddCommentView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key):
        issue = await _aget_issue(key)
        form = CommentForm(request.POST)
        if not await avalid(form):
            return redirect(issue.get_absolute_url())
        comment = form.save(commit=False)
        comment.issue = issue
        comment.author = request.user
        await comment.asave()
        if request.htmx:
            return await arender(request, "issues/_comment.html", {"c": comment})
        return redirect(issue.get_absolute_url())


class UploadAttachmentView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key):
        issue = await _aget_issue(key)
        f = request.FILES.get("file")
        if not f:
            return redirect(issue.get_absolute_url())
        att = await Attachment.objects.acreate(
            issue=issue, file=f, uploaded_by=request.user
        )
        if request.htmx:
            return await arender(request, "issues/_attachment.html", {"a": att})
        return redirect(issue.get_absolute_url())


class WatchToggleView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key):
        issue = await _aget_issue(key)
        if await issue.watchers.filter(pk=request.user.pk).aexists():
            await issue.watchers.aremove(request.user)
            watching = False
        else:
            await issue.watchers.aadd(request.user)
            watching = True
        if request.htmx:
            return await arender(
                request,
                "issues/_watch_button.html",
                {"issue": issue, "is_watching": watching},
            )
        return redirect(issue.get_absolute_url())
