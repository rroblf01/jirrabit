from asgiref.sync import sync_to_async
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest
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
from core.permissions import aassert_can_edit, aassert_can_view
from projects.models import Project

from .forms import CommentForm, IssueForm
from .inline import INLINE_FIELDS
from .models import Attachment, Comment, Issue, IssueLink, IssueType, Priority, Status, WorkLog


def _change_status_atomic(issue_pk, new_status_pk, user_pk):
    """Atomic status transition under row lock. Returns (issue, new_status, ok)."""
    from .models import HistoryEntry

    with transaction.atomic():
        issue = (
            Issue.objects
            .select_for_update()
            .select_related("status", "project")
            .get(pk=issue_pk)
        )
        new_status = Status.objects.get(pk=new_status_pk)
        current = issue.status
        if current.pk != new_status.pk and not current.can_transition_to(new_status):
            return issue, new_status, False
        issue.status = new_status
        if new_status.category == "done" and not issue.resolved_at:
            issue.resolved_at = timezone.now()
        elif new_status.category != "done":
            issue.resolved_at = None
        issue.save()
        if current.pk != new_status.pk:
            HistoryEntry.objects.create(
                issue=issue, actor_id=user_pk, field="status",
                old_value=str(current), new_value=str(new_status),
            )
    return issue, new_status, True


def _log_work_atomic(issue_pk, user_pk, minutes, comment):
    with transaction.atomic():
        issue = Issue.objects.select_for_update().get(pk=issue_pk)
        WorkLog.objects.create(
            issue=issue, author_id=user_pk, minutes=minutes, comment=comment,
        )
        issue.time_spent_minutes = (issue.time_spent_minutes or 0) + minutes
        if issue.time_remaining_minutes:
            issue.time_remaining_minutes = max(0, issue.time_remaining_minutes - minutes)
        issue.save(update_fields=[
            "time_spent_minutes", "time_remaining_minutes", "updated_at",
        ])
    return issue


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
        await aassert_can_edit(request.user, self.project)
        return await super().get(request, *args, **kwargs)

    async def post(self, request, *args, **kwargs):
        self.project = await _aget_project(kwargs["key"])
        await aassert_can_edit(request.user, self.project)
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
        issue = await _aget_issue(self.kwargs["key"])
        await aassert_can_view(self.request.user, issue.project)
        return issue

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        ctx["comment_form"] = CommentForm()
        ctx["statuses"] = [s async for s in Status.objects.all()]
        ctx["priorities"] = [p async for p in Priority.objects.all()]
        ctx["is_watching"] = await self.object.watchers.filter(
            pk=self.request.user.pk
        ).aexists()
        ctx["custom_fields"] = [
            f async for f in self.object.project.custom_fields.all()
        ]
        # N+1 fix: pre-evaluate related collections.
        ctx["comments"] = [
            c async for c in self.object.comments.select_related("author").all()
        ]
        ctx["labels"] = [l async for l in self.object.labels.all()]
        ctx["attachments"] = [
            a async for a in self.object.attachments.select_related("uploaded_by").all()
        ]
        ctx["history"] = [
            h async for h in self.object.history.select_related("actor")[:50]
        ]
        ctx["links"] = [
            l async for l in self.object.links_out.select_related("target", "target__status")
        ]
        worklogs_qs = self.object.worklogs.select_related("author").order_by("-logged_at")
        page = max(int(self.request.GET.get("page", "1")), 1)
        per_page = 10
        offset = (page - 1) * per_page
        ctx["worklogs"] = [w async for w in worklogs_qs[offset : offset + per_page]]
        ctx["worklogs_page"] = page
        ctx["worklogs_has_more"] = await worklogs_qs[
            offset + per_page : offset + per_page + 1
        ].aexists()
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
        await aassert_can_view(self.request.user, self.project)
        return (
            self.project.issues
            .select_related("status", "priority", "assignee", "issue_type")
            .prefetch_related("labels")
            .all()
        )

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        ctx["project"] = self.project
        return ctx


class ChangeStatusView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key):
        issue = await _aget_issue(key)
        await aassert_can_edit(request.user, issue.project)
        new_status = await _aget_status(request.POST.get("status"))
        issue, new_status, ok = await sync_to_async(
            _change_status_atomic, thread_sensitive=True,
        )(issue.pk, new_status.pk, request.user.pk)
        if not ok:
            return HttpResponseBadRequest(
                f"Transición no permitida hacia {new_status}"
            )
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
    MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024  # 5 MB raw

    async def post(self, request, key):
        import base64

        issue = await _aget_issue(key)
        await aassert_can_edit(request.user, issue.project)
        f = request.FILES.get("file")
        if not f:
            return redirect(issue.get_absolute_url())
        if f.size and f.size > self.MAX_ATTACHMENT_BYTES:
            return redirect(issue.get_absolute_url())
        payload = f.read()
        att = await Attachment.objects.acreate(
            issue=issue,
            uploaded_by=request.user,
            filename=f.name,
            content_type=getattr(f, "content_type", "") or "application/octet-stream",
            size=len(payload),
            data=base64.b64encode(payload).decode("ascii"),
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


# --- inline edit, comments, links, worklogs ---

async def _aget_comment(pk):
    try:
        return await Comment.objects.select_related("issue", "author").aget(pk=pk)
    except Comment.DoesNotExist as exc:
        raise Http404(f"Comment {pk} not found") from exc


class InlineEditFormView(AsyncLoginRequiredMixin, View):
    """Return the inline form fragment for ``field`` on issue ``key``."""

    async def get(self, request, key, field):
        if field not in INLINE_FIELDS:
            raise Http404("Unknown field")
        issue = await _aget_issue(key)
        await aassert_can_edit(request.user, issue.project)
        if request.GET.get("cancel"):
            handler = INLINE_FIELDS[field]
            return await arender(
                request, handler.display_template, {"issue": issue}
            )
        handler = INLINE_FIELDS[field]
        ctx = {"issue": issue, "field": field}
        ctx.update(await handler.context(issue))
        return await arender(request, handler.form_template, ctx)


class InlineEditApplyView(AsyncLoginRequiredMixin, View):
    """Apply the inline edit and return the display fragment."""

    async def post(self, request, key, field):
        if field not in INLINE_FIELDS:
            raise Http404("Unknown field")
        issue = await _aget_issue(key)
        await aassert_can_edit(request.user, issue.project)
        handler = INLINE_FIELDS[field]
        old, new = await handler.apply(issue, request)
        await issue.asave()
        from issues.models import HistoryEntry
        if old != new:
            await HistoryEntry.objects.acreate(
                issue=issue, actor=request.user, field=field,
                old_value=str(old)[:255], new_value=str(new)[:255],
            )
        return await arender(request, handler.display_template, {"issue": issue})


class CommentEditView(AsyncLoginRequiredMixin, View):
    async def get(self, request, pk):
        comment = await _aget_comment(pk)
        if comment.author_id != request.user.pk and not request.user.is_superuser:
            raise PermissionDenied
        return await arender(request, "issues/_comment_form.html", {"c": comment})

    async def post(self, request, pk):
        comment = await _aget_comment(pk)
        if comment.author_id != request.user.pk and not request.user.is_superuser:
            raise PermissionDenied
        new_body = request.POST.get("body", "").strip()
        if new_body and new_body != comment.body:
            comment.body = new_body
            comment.edited = True
            await comment.asave()
        return await arender(request, "issues/_comment.html", {"c": comment})


class CommentDeleteView(AsyncLoginRequiredMixin, View):
    async def post(self, request, pk):
        comment = await _aget_comment(pk)
        if comment.author_id != request.user.pk and not request.user.is_superuser:
            raise PermissionDenied
        await comment.adelete()
        return HttpResponse("")


class LogWorkView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key):
        issue = await _aget_issue(key)
        await aassert_can_edit(request.user, issue.project)
        raw = request.POST.get("minutes", "").strip()
        comment = request.POST.get("comment", "").strip()[:255]
        try:
            minutes = int(raw)
        except ValueError:
            return HttpResponseBadRequest("minutos inválidos")
        if minutes <= 0:
            return HttpResponseBadRequest("minutos debe ser > 0")
        issue = await sync_to_async(_log_work_atomic, thread_sensitive=True)(
            issue.pk, request.user.pk, minutes, comment,
        )
        worklogs = [w async for w in issue.worklogs.select_related("author")[:10]]
        return await arender(
            request, "issues/_time_panel.html",
            {"issue": issue, "worklogs": worklogs},
        )


class IssueLinkCreateView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key):
        issue = await _aget_issue(key)
        await aassert_can_edit(request.user, issue.project)
        target_key = request.POST.get("target", "").strip().upper()
        link_type = request.POST.get("type", "relates_to")
        if link_type not in dict(IssueLink.TYPE_CHOICES):
            return HttpResponseBadRequest("tipo inválido")
        target = await Issue.objects.filter(key=target_key).afirst()
        if not target or target.pk == issue.pk:
            return HttpResponseBadRequest("issue destino inválido")
        await IssueLink.objects.aget_or_create(
            source=issue, target=target, type=link_type,
            defaults={"created_by": request.user},
        )
        await IssueLink.objects.aget_or_create(
            source=target, target=issue, type=IssueLink.INVERSE[link_type],
            defaults={"created_by": request.user},
        )
        links = [l async for l in issue.links_out.select_related("target", "target__status")]
        return await arender(request, "issues/_links.html", {"issue": issue, "links": links})


class IssueLinkDeleteView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key, link_id):
        issue = await _aget_issue(key)
        await aassert_can_edit(request.user, issue.project)
        link = await IssueLink.objects.filter(pk=link_id, source=issue).afirst()
        if link:
            # remove inverse too
            await IssueLink.objects.filter(
                source=link.target, target=issue, type=IssueLink.INVERSE[link.type]
            ).adelete()
            await link.adelete()
        links = [l async for l in issue.links_out.select_related("target", "target__status")]
        return await arender(request, "issues/_links.html", {"issue": issue, "links": links})


class CustomFieldSetView(AsyncLoginRequiredMixin, View):
    """Persist a value into ``Issue.custom_fields[slug]`` (JSON)."""

    async def post(self, request, key, slug):
        issue = await _aget_issue(key)
        await aassert_can_edit(request.user, issue.project)
        value = request.POST.get("value", "")
        cf = issue.custom_fields or {}
        cf[slug] = value
        issue.custom_fields = cf
        await issue.asave(update_fields=["custom_fields", "updated_at"])
        return await arender(
            request, "issues/_custom_field.html",
            {"issue": issue, "slug": slug, "value": value},
        )
