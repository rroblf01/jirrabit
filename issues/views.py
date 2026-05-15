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

from .forms import CommentForm, IssueForm, IssueTemplateForm
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
        from .models import IssueTemplate

        if self.request.method == "POST":
            return IssueForm(self.request.POST, project=self.project)
        status = await Status.objects.order_by("order").afirst()
        priority = await Priority.objects.afirst()
        initial: dict = {"status": status, "priority": priority}

        # ``?template=<pk>`` wins over ``?type``: pre-fill from a saved template.
        tpl_param = self.request.GET.get("template")
        tpl = None
        if tpl_param and tpl_param.isdigit():
            tpl = await IssueTemplate.objects.filter(
                pk=tpl_param, project=self.project,
            ).select_related("issue_type", "priority").afirst()
        if tpl is not None:
            initial["issue_type"] = tpl.issue_type
            initial["summary"] = tpl.summary
            initial["description"] = tpl.description
            if tpl.priority_id:
                initial["priority"] = tpl.priority
            initial["labels"] = [l async for l in tpl.labels.all()]
            return IssueForm(project=self.project, initial=initial)

        type_param = self.request.GET.get("type")
        if type_param and type_param.isdigit():
            itype = await IssueType.objects.filter(pk=type_param).afirst()
        else:
            itype = None
        if itype is None:
            itype = await IssueType.objects.afirst()
        initial["issue_type"] = itype
        initial["description"] = itype.description_template if itype else ""
        return IssueForm(project=self.project, initial=initial)

    async def aget_context_data(self, **kwargs):
        from .models import IssueTemplate

        ctx = await super().aget_context_data(**kwargs)
        ctx["project"] = self.project
        ctx["templates"] = [
            t async for t in IssueTemplate.objects.filter(project=self.project)
        ]
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
        # Track recently viewed for the home dashboard.
        from .models import Visit
        await Visit.objects.aupdate_or_create(user=self.request.user, issue=issue)
        # Mark any pending mention receipts as seen.
        from accounts.models import MentionReceipt
        await MentionReceipt.objects.filter(
            mentioned=self.request.user, comment__issue=issue, seen_at__isnull=True,
        ).aupdate(seen_at=timezone.now())
        return issue

    async def aget_context_data(self, **kwargs):
        from .models import NotificationSnooze, Pin, Timer
        ctx = await super().aget_context_data(**kwargs)
        ctx["comment_form"] = CommentForm()
        ctx["is_pinned"] = await Pin.objects.filter(
            user=self.request.user, issue=self.object,
        ).aexists()
        snooze = await NotificationSnooze.objects.filter(
            user=self.request.user, issue=self.object, until__gt=timezone.now(),
        ).afirst()
        ctx["snoozed_until"] = snooze.until if snooze else None
        ctx["timer_running"] = await Timer.objects.filter(
            user=self.request.user, issue=self.object,
        ).aexists()
        ctx["subtasks"] = [
            s async for s in
            self.object.subtasks.select_related("status", "assignee", "issue_type").order_by("key")
        ]
        ctx["branches"] = [
            b async for b in self.object.branches.all().order_by("-created_at")
        ]
        ctx["statuses"] = [s async for s in Status.objects.all()]
        ctx["priorities"] = [p async for p in Priority.objects.all()]
        ctx["is_watching"] = await self.object.watchers.filter(
            pk=self.request.user.pk
        ).aexists()
        ctx["custom_fields"] = [
            f async for f in self.object.project.custom_fields.all()
        ]
        # N+1 fix: pre-evaluate related collections.
        comments_qs = self.object.comments.select_related("author").filter(deleted_at__isnull=True)
        if not (self.request.user.is_staff or self.request.user.is_superuser):
            comments_qs = comments_qs.filter(is_internal=False)
        ctx["comments"] = [c async for c in comments_qs]
        # Pre-aggregate reactions for each comment to avoid N+1 in the template.
        from asgiref.sync import sync_to_async as _sta
        for c in ctx["comments"]:
            c.reactions_agg = await _sta(_aggregate_reactions, thread_sensitive=True)(
                c.pk, self.request.user.pk,
            )
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
        ctx["csv_columns"] = IssueCsvExportView.ALL_COLUMNS
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
        if not (request.user.is_staff or request.user.is_superuser):
            comment.is_internal = False
        parent_id = request.POST.get("parent", "").strip()
        if parent_id and parent_id.isdigit():
            parent = await Comment.objects.filter(pk=int(parent_id), issue=issue).afirst()
            if parent is not None:
                comment.parent = parent
        await comment.asave()
        # Auto-watch: anyone who comments starts following the issue.
        await issue.watchers.aadd(request.user)
        # ``and_close`` checkbox triggers a status advance to first ``done``
        # category status in one shot ("Comentar y cerrar").
        if request.POST.get("and_close"):
            done = await Status.objects.filter(category="done").order_by("order").afirst()
            if done and issue.status_id != done.pk:
                await sync_to_async(
                    _change_status_atomic, thread_sensitive=True,
                )(issue.pk, done.pk, request.user.pk)
            resp = HttpResponse(status=204)
            resp["HX-Refresh"] = "true"
            return resp
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
        from .models import CommentEdit
        comment = await _aget_comment(pk)
        if comment.author_id != request.user.pk and not request.user.is_superuser:
            raise PermissionDenied
        new_body = request.POST.get("body", "").strip()
        if new_body and new_body != comment.body:
            # Snapshot the previous version for the history viewer.
            await CommentEdit.objects.acreate(
                comment=comment, old_body=comment.body, edited_by=request.user,
            )
            comment.body = new_body
            comment.edited = True
            await comment.asave()
        return await arender(request, "issues/_comment.html", {"c": comment})


class CommentDeleteView(AsyncLoginRequiredMixin, View):
    async def post(self, request, pk):
        from django.urls import reverse

        comment = await _aget_comment(pk)
        if comment.author_id != request.user.pk and not request.user.is_superuser:
            raise PermissionDenied
        comment.deleted_at = timezone.now()
        await comment.asave(update_fields=["deleted_at", "updated_at"])
        resp = HttpResponse("")
        resp["HX-Trigger"] = "commentDeleted"
        resp["X-Undo-URL"] = reverse("issues:comment_restore", args=[pk])
        resp["X-Undo-Message"] = "Comentario borrado"
        return resp


class CommentRestoreView(AsyncLoginRequiredMixin, View):
    """Undo target for a recently soft-deleted comment."""

    async def post(self, request, pk):
        comment = await _aget_comment(pk)
        if comment.author_id != request.user.pk and not request.user.is_superuser:
            raise PermissionDenied
        if not comment.deleted_at:
            return HttpResponse("ya restaurado", status=200)
        comment.deleted_at = None
        await comment.asave(update_fields=["deleted_at", "updated_at"])
        return await arender(request, "issues/_comment.html", {"c": comment})


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


class AdvanceStatusView(AsyncLoginRequiredMixin, View):
    """Advance an issue to its next allowed status with one click.

    If the current status has exactly one entry in ``allowed_next``, go there.
    Otherwise pick the lowest-``order`` status from ``allowed_next``. If
    ``allowed_next`` is empty (open workflow), pick the lowest-``order``
    status whose ``order > current.order`` and category != current.category.
    """

    async def post(self, request, key):
        issue = await _aget_issue(key)
        await aassert_can_edit(request.user, issue.project)
        current = issue.status
        candidates = [s async for s in current.allowed_next.all().order_by("order", "id")]
        if not candidates:
            # Open workflow — pick next by global order.
            candidates = [
                s async for s in
                Status.objects.filter(order__gt=current.order).order_by("order", "id")[:1]
            ]
        if not candidates:
            return HttpResponseBadRequest("no hay siguiente estado")
        target = candidates[0]
        issue, target, ok = await sync_to_async(
            _change_status_atomic, thread_sensitive=True,
        )(issue.pk, target.pk, request.user.pk)
        if not ok:
            return HttpResponseBadRequest("transición no permitida")
        if request.htmx:
            if request.headers.get("X-Source") == "board":
                return await arender(
                    request, "board/_card_advance.html", {"issue": issue},
                )
            statuses = [s async for s in Status.objects.all()]
            return await arender(
                request, "issues/_status_badge.html",
                {"issue": issue, "statuses": statuses},
            )
        return redirect(issue.get_absolute_url())


class PinToggleView(AsyncLoginRequiredMixin, View):
    """Toggle a pin on an issue (or project) for the current user."""

    async def post(self, request, kind, pk):
        from .models import Pin
        if kind == "issue":
            if not await Issue.objects.filter(pk=pk).aexists():
                raise Http404
            existing = await Pin.objects.filter(user=request.user, issue_id=pk).afirst()
            pinned = False
            if existing:
                await existing.adelete()
            else:
                await Pin.objects.acreate(user=request.user, issue_id=pk)
                pinned = True
        elif kind == "project":
            existing = await Pin.objects.filter(user=request.user, project_id=pk).afirst()
            pinned = False
            if existing:
                await existing.adelete()
            else:
                await Pin.objects.acreate(user=request.user, project_id=pk)
                pinned = True
        else:
            return HttpResponseBadRequest("kind inválido")
        if request.htmx:
            return await arender(
                request, "issues/_pin_button.html",
                {"kind": kind, "pk": pk, "pinned": pinned},
            )
        return HttpResponse(status=204)


class SnoozeView(AsyncLoginRequiredMixin, View):
    """Silence notifications for an issue for the given duration in hours."""

    async def post(self, request, key):
        from datetime import timedelta
        from .models import NotificationSnooze
        issue = await _aget_issue(key)
        try:
            hours = int(request.POST.get("hours", "24"))
        except ValueError:
            hours = 24
        hours = max(1, min(hours, 24 * 30))
        until = timezone.now() + timedelta(hours=hours)
        await NotificationSnooze.objects.aupdate_or_create(
            user=request.user, issue=issue, defaults={"until": until},
        )
        if request.htmx:
            return await arender(
                request, "issues/_snooze_button.html",
                {"issue": issue, "snoozed_until": until},
            )
        return redirect(issue.get_absolute_url())


class UnsnoozeView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key):
        from .models import NotificationSnooze
        issue = await _aget_issue(key)
        await NotificationSnooze.objects.filter(user=request.user, issue=issue).adelete()
        if request.htmx:
            return await arender(
                request, "issues/_snooze_button.html",
                {"issue": issue, "snoozed_until": None},
            )
        return redirect(issue.get_absolute_url())


class StartWorkView(AsyncLoginRequiredMixin, View):
    """One-click ``Start work`` combo.

    - Self-assigns the issue to ``request.user`` (if member of the project).
    - Moves status to the first ``in_progress`` category status (if not yet).
    - Starts a timer (auto-stops any previously running one).

    Returns JSON so the client can refresh the relevant chunks via HX-Trigger.
    """

    async def post(self, request, key):
        import json
        from .models import Timer

        issue = await _aget_issue(key)
        await aassert_can_edit(request.user, issue.project)

        # 1. Self-assign (skip if already assignee).
        if issue.assignee_id != request.user.pk:
            issue.assignee = request.user
            await issue.asave(update_fields=["assignee", "updated_at"])

        # 2. Move to in_progress (first one in workflow).
        target = await Status.objects.filter(category="in_progress").order_by("order").afirst()
        moved = False
        if target and issue.status_id != target.pk:
            _, _, ok = await sync_to_async(
                _change_status_atomic, thread_sensitive=True,
            )(issue.pk, target.pk, request.user.pk)
            moved = bool(ok)

        # 3. Start timer.
        prev = await Timer.objects.filter(user=request.user).select_related("issue").afirst()
        if prev and prev.issue_id != issue.pk:
            await _astop_timer(prev)
        if not await Timer.objects.filter(user=request.user, issue=issue).aexists():
            await Timer.objects.acreate(user=request.user, issue=issue)

        resp = HttpResponse(
            json.dumps({"ok": True, "assigned": True, "moved": moved}),
            content_type="application/json",
        )
        resp["HX-Refresh"] = "true"
        return resp


class TimerStartView(AsyncLoginRequiredMixin, View):
    """Start a timer on this issue. Stops any other running timer first."""

    async def post(self, request, key):
        from .models import Timer
        issue = await _aget_issue(key)
        await aassert_can_edit(request.user, issue.project)
        # Auto-stop the previous timer (commits a WorkLog).
        prev = await Timer.objects.filter(user=request.user).select_related("issue").afirst()
        if prev:
            await _astop_timer(prev)
        await Timer.objects.acreate(user=request.user, issue=issue)
        if request.htmx:
            return await arender(request, "issues/_timer_button.html",
                                 {"issue": issue, "running": True})
        return redirect(issue.get_absolute_url())


class TimerStopView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key):
        from .models import Timer
        issue = await _aget_issue(key)
        timer = await Timer.objects.filter(user=request.user, issue=issue).afirst()
        if timer:
            await _astop_timer(timer)
        if request.htmx:
            return await arender(request, "issues/_timer_button.html",
                                 {"issue": issue, "running": False})
        return redirect(issue.get_absolute_url())


async def _astop_timer(timer):
    """Convert a running ``Timer`` into a ``WorkLog`` and delete it."""
    elapsed = (timezone.now() - timer.started_at).total_seconds()
    minutes = max(1, int(round(elapsed / 60)))
    await sync_to_async(_log_work_atomic, thread_sensitive=True)(
        timer.issue_id, timer.user_id, minutes, "(timer)",
    )
    await timer.adelete()


class IssueCloneView(AsyncLoginRequiredMixin, View):
    """Duplicate an issue (summary, description, type, priority, assignee,
    labels, epic). Subtasks, comments, history, attachments and links are
    not copied — the clone starts fresh."""

    async def post(self, request, key):
        src = await _aget_issue(key)
        await aassert_can_edit(request.user, src.project)
        num = await src.project.anext_issue_number()
        clone = Issue(
            project=src.project,
            reporter=request.user,
            summary=f"[clon] {src.summary}",
            description=src.description,
            status=src.status,
            priority=src.priority,
            issue_type=src.issue_type,
            assignee=src.assignee,
            epic=src.epic,
            story_points=src.story_points,
            estimate_minutes=src.estimate_minutes,
            due_date=src.due_date,
            key=f"{src.project.key}-{num}",
        )
        await clone.asave()
        # Copy labels (M2M).
        async for lab in src.labels.all():
            await clone.labels.aadd(lab)
        return redirect(clone.get_absolute_url())


class SubtaskCreateView(AsyncLoginRequiredMixin, View):
    """Create a subtask under ``key`` from a single summary input."""

    async def post(self, request, key):
        parent = await _aget_issue(key)
        await aassert_can_edit(request.user, parent.project)
        summary = request.POST.get("summary", "").strip()
        if not summary:
            return HttpResponseBadRequest("summary requerido")
        subtype = await IssueType.objects.filter(category="subtask").afirst() \
            or await IssueType.objects.afirst()
        default_status = await Status.objects.order_by("order").afirst()
        default_prio = await Priority.objects.afirst()
        num = await parent.project.anext_issue_number()
        sub = Issue(
            project=parent.project, parent=parent, reporter=request.user,
            summary=summary[:255], description="",
            status=default_status, priority=default_prio, issue_type=subtype,
            key=f"{parent.project.key}-{num}",
        )
        await sub.asave()
        # Render the subtasks fragment for HTMX swap.
        subtasks = [
            s async for s in
            parent.subtasks.select_related("status", "assignee", "issue_type").order_by("key")
        ]
        return await arender(
            request, "issues/_subtasks.html",
            {"issue": parent, "subtasks": subtasks},
        )


class SubtaskToggleView(AsyncLoginRequiredMixin, View):
    """Flip a subtask between its first 'todo' status and the project's 'done'.

    Used by the checkbox in the parent's subtask checklist. Bypasses
    workflow restrictions intentionally — this is a power-user shortcut.
    """

    async def post(self, request, key):
        sub = await _aget_issue(key)
        await aassert_can_edit(request.user, sub.project)
        if sub.status.category == "done":
            target = await Status.objects.exclude(category="done").order_by("order").afirst()
        else:
            target = await Status.objects.filter(category="done").afirst() \
                or await Status.objects.order_by("-order").afirst()
        if target is None:
            return HttpResponseBadRequest("sin estado destino")
        sub, target, ok = await sync_to_async(
            _change_status_atomic, thread_sensitive=True,
        )(sub.pk, target.pk, request.user.pk)
        if sub.parent_id:
            parent = await _aget_issue_by_pk(sub.parent_id)
            subtasks = [
                s async for s in
                parent.subtasks.select_related("status", "assignee", "issue_type").order_by("key")
            ]
            return await arender(
                request, "issues/_subtasks.html",
                {"issue": parent, "subtasks": subtasks},
            )
        return HttpResponse(status=204)


async def _aget_issue_by_pk(pk):
    qs = Issue.objects.select_related("project", "status")
    try:
        return await qs.aget(pk=pk)
    except Issue.DoesNotExist as exc:
        raise Http404 from exc


class ReactToggleView(AsyncLoginRequiredMixin, View):
    """Toggle an emoji reaction on a comment for the current user."""

    async def post(self, request, comment_id):
        from .models import Reaction
        emoji = request.POST.get("emoji", "").strip()
        valid = {e for e, _ in Reaction.EMOJIS}
        if emoji not in valid:
            return HttpResponseBadRequest("emoji inválido")
        comment = await Comment.objects.select_related("issue", "issue__project").aget(pk=comment_id)
        await aassert_can_view(request.user, comment.issue.project)
        existing = await Reaction.objects.filter(
            comment=comment, user=request.user, emoji=emoji,
        ).afirst()
        if existing:
            await existing.adelete()
        else:
            await Reaction.objects.acreate(comment=comment, user=request.user, emoji=emoji)
        # Broadcast to the project group so other viewers see it live.
        try:
            from asgiref.sync import sync_to_async as _sta
            from channels.layers import get_channel_layer
            layer = get_channel_layer()
            if layer:
                await layer.group_send(
                    f"project.{comment.issue.project.key}",
                    {"type": "reaction.event",
                     "payload": {"comment_id": comment.pk}},
                )
        except Exception:
            pass
        reactions = await sync_to_async(_aggregate_reactions, thread_sensitive=True)(
            comment.pk, request.user.pk,
        )
        return await arender(
            request, "issues/_reactions.html",
            {"c": comment, "reactions": reactions},
        )


def _aggregate_reactions(comment_id, viewer_pk):
    """Group reactions by emoji with counts and whether the viewer is in."""
    from .models import Reaction
    rows = list(
        Reaction.objects.filter(comment_id=comment_id)
        .values("emoji", "user_id")
    )
    by_emoji: dict[str, dict] = {}
    for r in rows:
        bucket = by_emoji.setdefault(r["emoji"], {"emoji": r["emoji"], "count": 0, "mine": False})
        bucket["count"] += 1
        if r["user_id"] == viewer_pk:
            bucket["mine"] = True
    label_map = {e: lbl for e, lbl in __import__("issues.models", fromlist=["Reaction"]).Reaction.EMOJIS}
    for b in by_emoji.values():
        b["label"] = label_map.get(b["emoji"], b["emoji"])
    return list(by_emoji.values())


class BranchLinkCreateView(AsyncLoginRequiredMixin, View):
    """Attach a git branch reference to an issue."""

    async def post(self, request, key):
        from .models import BranchLink
        issue = await _aget_issue(key)
        await aassert_can_edit(request.user, issue.project)
        branch = request.POST.get("branch", "").strip()[:200]
        if not branch:
            return HttpResponseBadRequest("branch requerido")
        repo = request.POST.get("repo_url", "").strip()[:255]
        sha = request.POST.get("commit_sha", "").strip()[:64]
        msg = request.POST.get("message", "").strip()[:255]
        await BranchLink.objects.aget_or_create(
            issue=issue, branch=branch, commit_sha=sha,
            defaults={"repo_url": repo, "message": msg, "created_by": request.user},
        )
        links = [b async for b in issue.branches.all().order_by("-created_at")]
        return await arender(
            request, "issues/_branch_links.html",
            {"issue": issue, "branches": links},
        )


class BranchLinkDeleteView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key, pk):
        from .models import BranchLink
        issue = await _aget_issue(key)
        await aassert_can_edit(request.user, issue.project)
        await BranchLink.objects.filter(pk=pk, issue=issue).adelete()
        links = [b async for b in issue.branches.all().order_by("-created_at")]
        return await arender(
            request, "issues/_branch_links.html",
            {"issue": issue, "branches": links},
        )


class CommentEditHistoryView(AsyncLoginRequiredMixin, View):
    """Render the list of prior bodies for a comment."""

    async def get(self, request, pk):
        comment = await _aget_comment(pk)
        await aassert_can_view(request.user, comment.issue.project)
        edits = [e async for e in comment.edits.all().order_by("-edited_at")]
        return await arender(
            request, "issues/_comment_history.html",
            {"comment": comment, "edits": edits},
        )


class IssueCsvImportView(AsyncLoginRequiredMixin, View):
    """Paste CSV → preview → bulk-create issues for a project.

    Expected columns (case-insensitive): ``summary`` (required), ``description``,
    ``type``, ``priority``, ``assignee``, ``story_points``, ``due_date``.
    Unknown columns are ignored. Type/priority/status fall back to first
    available row in the global tables when the value is missing.
    """

    template_name = "issues/csv_import.html"

    async def get(self, request, key):
        project = await _aget_project(key)
        await aassert_can_edit(request.user, project)
        return await arender(request, self.template_name,
                             {"project": project, "preview": None, "csv_text": ""})

    async def post(self, request, key):
        import csv as _csv
        import io
        from datetime import date as _date

        project = await _aget_project(key)
        await aassert_can_edit(request.user, project)
        text = request.POST.get("csv", "").strip()
        if not text:
            return await arender(request, self.template_name,
                                 {"project": project, "preview": None, "csv_text": "", "error": "CSV vacío"})

        reader = _csv.DictReader(io.StringIO(text))
        # Normalize header to lowercase.
        rows = []
        for raw in reader:
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            if not row.get("summary"):
                continue
            rows.append(row)
        if not rows:
            return await arender(request, self.template_name,
                                 {"project": project, "preview": None, "csv_text": text, "error": "Sin filas válidas (summary requerido)"})

        action = request.POST.get("action", "preview")
        if action == "preview":
            return await arender(request, self.template_name,
                                 {"project": project, "preview": rows[:50], "csv_text": text,
                                  "total": len(rows)})

        # Action == import: build issues.
        default_status = await Status.objects.order_by("order").afirst()
        types_by_name = {t.name.lower(): t async for t in IssueType.objects.all()}
        priorities_by_name = {p.name.lower(): p async for p in Priority.objects.all()}
        default_type = next(iter(types_by_name.values())) if types_by_name else None
        default_priority = next(iter(priorities_by_name.values())) if priorities_by_name else None
        from accounts.models import User
        usernames = {row["assignee"].lower() for row in rows if row.get("assignee")}
        users_by_name = {
            u.username.lower(): u async for u in
            User.objects.filter(username__in=usernames)
        } if usernames else {}

        created = 0
        for row in rows:
            try:
                sp = int(row["story_points"]) if row.get("story_points") else None
            except ValueError:
                sp = None
            due = None
            if row.get("due_date"):
                try:
                    due = _date.fromisoformat(row["due_date"])
                except ValueError:
                    due = None
            itype = types_by_name.get(row.get("type", "").lower(), default_type)
            prio = priorities_by_name.get(row.get("priority", "").lower(), default_priority)
            assignee = users_by_name.get(row.get("assignee", "").lower())
            num = await project.anext_issue_number()
            issue = Issue(
                project=project,
                reporter=request.user,
                summary=row["summary"][:255],
                description=row.get("description", ""),
                status=default_status, priority=prio, issue_type=itype,
                assignee=assignee, story_points=sp, due_date=due,
                key=f"{project.key}-{num}",
            )
            await issue.asave()
            created += 1
        from django.contrib import messages
        messages.success(request, f"{created} tareas importadas.")
        return redirect("issues:list", key=project.key)


class IssueCsvExportView(AsyncLoginRequiredMixin, View):
    """Stream issues of a project as CSV, honoring the same filters as the list view.

    ``?cols=`` accepts a comma-separated subset of the columns below. Without
    that param the full set is exported (backwards-compatible).
    """

    ALL_COLUMNS = (
        "key", "summary", "status", "priority", "type",
        "assignee", "reporter", "sprint", "epic",
        "story_points", "estimate_minutes", "time_spent_minutes",
        "due_date", "resolved_at", "created_at", "updated_at",
    )

    def _row(self, i, columns):
        out = []
        for c in columns:
            if c == "key": out.append(i.key)
            elif c == "summary": out.append(i.summary)
            elif c == "status": out.append(str(i.status))
            elif c == "priority": out.append(str(i.priority))
            elif c == "type": out.append(str(i.issue_type))
            elif c == "assignee": out.append(getattr(i.assignee, "username", "") or "")
            elif c == "reporter": out.append(getattr(i.reporter, "username", "") or "")
            elif c == "sprint": out.append(getattr(i.sprint, "name", "") or "")
            elif c == "epic": out.append(getattr(i.epic, "name", "") or "")
            elif c == "story_points": out.append(i.story_points if i.story_points is not None else "")
            elif c == "estimate_minutes": out.append(i.estimate_minutes if i.estimate_minutes is not None else "")
            elif c == "time_spent_minutes": out.append(i.time_spent_minutes or 0)
            elif c == "due_date": out.append(i.due_date.isoformat() if i.due_date else "")
            elif c == "resolved_at": out.append(i.resolved_at.isoformat() if i.resolved_at else "")
            elif c == "created_at": out.append(i.created_at.isoformat())
            elif c == "updated_at": out.append(i.updated_at.isoformat())
            else: out.append("")
        return out

    async def get(self, request, key):
        import csv
        import io

        project = await _aget_project(key)
        await aassert_can_view(request.user, project)

        cols_param = request.GET.get("cols", "").strip()
        if cols_param:
            allowed = set(self.ALL_COLUMNS)
            columns = [c for c in cols_param.split(",") if c in allowed] or list(self.ALL_COLUMNS)
        else:
            columns = list(self.ALL_COLUMNS)

        qs = project.issues.select_related(
            "status", "priority", "issue_type", "assignee", "reporter", "sprint", "epic",
        ).order_by("key")

        text = request.GET.get("text", "").strip()
        status_id = request.GET.get("status")
        assignee = request.GET.get("assignee")
        if text:
            from django.db.models import Q
            qs = qs.filter(Q(summary__icontains=text) | Q(key__icontains=text))
        if status_id and status_id.isdigit():
            qs = qs.filter(status_id=int(status_id))
        if assignee == "me":
            qs = qs.filter(assignee=request.user)
        elif assignee and assignee.isdigit():
            qs = qs.filter(assignee_id=int(assignee))
        if not request.GET.get("archived"):
            qs = qs.filter(archived=False)

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        async for i in qs:
            writer.writerow(self._row(i, columns))
        resp = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = (
            f'attachment; filename="{project.key}-issues-{timezone.now():%Y%m%d}.csv"'
        )
        return resp


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


class IssueJsonExportView(AsyncLoginRequiredMixin, View):
    """JSON dump of issues for a project. Mirrors CSV columns 1:1."""

    async def get(self, request, key):
        import json

        project = await _aget_project(key)
        await aassert_can_view(request.user, project)

        qs = project.issues.select_related(
            "status", "priority", "issue_type", "assignee", "reporter", "sprint", "epic",
        ).order_by("key")
        if not request.GET.get("archived"):
            qs = qs.filter(archived=False)

        items = []
        async for i in qs:
            items.append({
                "key": i.key,
                "summary": i.summary,
                "status": str(i.status),
                "priority": str(i.priority),
                "type": str(i.issue_type),
                "assignee": getattr(i.assignee, "username", None),
                "reporter": getattr(i.reporter, "username", None),
                "sprint": getattr(i.sprint, "name", None),
                "epic": getattr(i.epic, "name", None),
                "story_points": i.story_points,
                "due_date": i.due_date.isoformat() if i.due_date else None,
                "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None,
                "created_at": i.created_at.isoformat(),
                "updated_at": i.updated_at.isoformat(),
                "url": request.build_absolute_uri(i.get_absolute_url()),
            })
        resp = HttpResponse(
            json.dumps({"project": project.key, "issues": items}, ensure_ascii=False),
            content_type="application/json; charset=utf-8",
        )
        resp["Content-Disposition"] = (
            f'attachment; filename="{project.key}-issues-{timezone.now():%Y%m%d}.json"'
        )
        return resp


class IssueTemplateListView(AsyncLoginRequiredMixin, AsyncListView):
    template_name = "issues/template_list.html"
    context_object_name = "templates"

    async def aget_queryset(self):
        from .models import IssueTemplate
        self.project = await _aget_project(self.kwargs["key"])
        await aassert_can_view(self.request.user, self.project)
        return IssueTemplate.objects.filter(project=self.project).select_related("issue_type")

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        ctx["project"] = self.project
        return ctx


class IssueTemplateCreateView(AsyncLoginRequiredMixin, AsyncCreateView):
    form_class = IssueTemplateForm
    template_name = "issues/template_form.html"

    async def get(self, request, *args, **kwargs):
        self.project = await _aget_project(kwargs["key"])
        await aassert_can_edit(request.user, self.project)
        return await super().get(request, *args, **kwargs)

    async def post(self, request, *args, **kwargs):
        self.project = await _aget_project(kwargs["key"])
        await aassert_can_edit(request.user, self.project)
        return await super().post(request, *args, **kwargs)

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        ctx["project"] = self.project
        return ctx

    async def aform_valid(self, form):
        tpl = form.save(commit=False)
        tpl.project = self.project
        tpl.created_by = self.request.user
        await tpl.asave()
        await tpl.labels.aset(form.cleaned_data.get("labels", []))
        self.object = tpl
        return redirect("issues:template_list", key=self.project.key)


class IssueTemplateUpdateView(AsyncLoginRequiredMixin, AsyncUpdateView):
    form_class = IssueTemplateForm
    template_name = "issues/template_form.html"

    async def aget_object(self):
        from .models import IssueTemplate
        self.project = await _aget_project(self.kwargs["key"])
        await aassert_can_edit(self.request.user, self.project)
        return await IssueTemplate.objects.filter(
            pk=self.kwargs["pk"], project=self.project,
        ).afirst()

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        ctx["project"] = self.project
        return ctx

    async def aform_valid(self, form):
        tpl = form.save(commit=False)
        await tpl.asave()
        await tpl.labels.aset(form.cleaned_data.get("labels", []))
        self.object = tpl
        return redirect("issues:template_list", key=self.project.key)


class IssueTemplateDeleteView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key, pk):
        from .models import IssueTemplate
        project = await _aget_project(key)
        await aassert_can_edit(request.user, project)
        await IssueTemplate.objects.filter(pk=pk, project=project).adelete()
        return redirect("issues:template_list", key=project.key)
