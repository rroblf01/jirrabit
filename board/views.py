from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from django.views import View

from core.aio import arender
from core.async_views import AsyncTemplateView
from core.mixins import AsyncLoginRequiredMixin
from core.permissions import aassert_can_edit, aassert_can_view
from issues.models import Issue, Status
from projects.models import Project


async def _aget_project(key):
    try:
        return await Project.objects.aget(key=key)
    except Project.DoesNotExist as exc:
        raise Http404(f"Project '{key}' not found") from exc


async def _filtered_issues_qs(project, request):
    qs = project.issues.select_related("status", "priority", "issue_type", "assignee")
    if not request.GET.get("archived"):
        qs = qs.filter(archived=False)
    assignee = request.GET.get("assignee")
    if assignee == "me":
        qs = qs.filter(assignee=request.user)
    elif assignee == "none":
        qs = qs.filter(assignee__isnull=True)
    elif assignee and assignee.isdigit():
        qs = qs.filter(assignee_id=int(assignee))
    itype = request.GET.get("type")
    if itype and itype.isdigit():
        qs = qs.filter(issue_type_id=int(itype))
    prio = request.GET.get("priority")
    if prio and prio.isdigit():
        qs = qs.filter(priority_id=int(prio))
    epic = request.GET.get("epic")
    if epic and epic.isdigit():
        qs = qs.filter(epic_id=int(epic))
    text = request.GET.get("text", "").strip()
    if text:
        from django.db.models import Q
        qs = qs.filter(Q(summary__icontains=text) | Q(key__icontains=text))
    stale = request.GET.get("stale")
    if stale and stale.isdigit():
        from datetime import timedelta

        from django.utils import timezone
        cutoff = timezone.now() - timedelta(days=int(stale))
        qs = qs.filter(updated_at__lt=cutoff).exclude(status__category="done")
    due = request.GET.get("due")
    if due == "overdue":
        from django.utils import timezone
        qs = qs.filter(due_date__lt=timezone.localdate()).exclude(status__category="done")
    if request.GET.get("urgent"):
        qs = qs.filter(priority__weight__gte=40)
    if request.GET.get("review"):
        qs = qs.filter(status__name__iexact="In Review")
    if request.GET.get("recent"):
        from datetime import timedelta

        from django.utils import timezone
        qs = qs.filter(updated_at__gte=timezone.now() - timedelta(hours=24))
    return qs


class BoardView(AsyncLoginRequiredMixin, AsyncTemplateView):
    template_name = "board/board.html"

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        project = await _aget_project(self.kwargs["key"])
        await aassert_can_view(self.request.user, project)
        sprint_id = self.request.GET.get("sprint")
        if sprint_id and sprint_id != "all":
            issues_qs = (await _filtered_issues_qs(project, self.request)).filter(sprint_id=sprint_id)
            active_sprint = await project.sprints.filter(pk=sprint_id).afirst()
        elif sprint_id == "all":
            issues_qs = await _filtered_issues_qs(project, self.request)
            active_sprint = None
        else:
            active_sprint = await project.sprints.filter(status="active").afirst()
            base_qs = await _filtered_issues_qs(project, self.request)
            issues_qs = base_qs.filter(sprint=active_sprint) if active_sprint else base_qs

        statuses = [s async for s in Status.objects.all()]
        all_issues = [i async for i in issues_qs.order_by("rank", "-updated_at")]
        ctx["project"] = project
        ctx["columns"] = [
            {"status": s, "issues": [i for i in all_issues if i.status_id == s.pk]}
            for s in statuses
        ]
        ctx["active_sprint"] = active_sprint
        ctx["sprints"] = [s async for s in project.sprints.all()]
        ctx["filter_assignee"] = self.request.GET.get("assignee", "")
        ctx["filter_type"] = self.request.GET.get("type", "")
        ctx["filter_priority"] = self.request.GET.get("priority", "")
        ctx["filter_epic"] = self.request.GET.get("epic", "")
        ctx["filter_text"] = self.request.GET.get("text", "")
        from issues.models import IssueType, Priority
        ctx["types"] = [t async for t in IssueType.objects.all()]
        ctx["priorities"] = [p async for p in Priority.objects.all()]
        ctx["epics"] = [e async for e in project.epics.filter(done=False)]
        ctx["members"] = [m async for m in project.members.defer("avatar").all()]
        from issues.models import Label
        ctx["labels"] = [lab async for lab in Label.objects.all().order_by("name")]
        return ctx


class MoveCardView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key):
        try:
            issue = await Issue.objects.select_related("status", "project").aget(key=key)
        except Issue.DoesNotExist as exc:
            raise Http404(f"Issue '{key}' not found") from exc
        await aassert_can_edit(request.user, issue.project)
        status_id = request.POST.get("status")
        if not status_id:
            return HttpResponseBadRequest("status required")
        try:
            status = await Status.objects.aget(pk=status_id)
        except Status.DoesNotExist as exc:
            raise Http404(f"Status {status_id} not found") from exc
        # Delegate to the canonical atomic transition so we get row locks,
        # workflow validation, HistoryEntry creation and resolved_at
        # bookkeeping for free (mirrors ChangeStatusView / AdvanceStatusView).
        from asgiref.sync import sync_to_async

        from issues.views import _change_status_atomic

        issue, status, ok = await sync_to_async(
            _change_status_atomic, thread_sensitive=True,
        )(issue.pk, status.pk, request.user.pk)
        if not ok:
            return HttpResponseBadRequest(f"Transición no permitida: {issue.status} → {status}")
        return await arender(request, "board/_card.html", {"issue": issue})


class BulkUpdateView(AsyncLoginRequiredMixin, View):
    """Apply the same change to multiple issues at once.

    Form fields:
      - ``keys[]``: list of issue keys
      - ``action``: status|assignee|sprint|priority|epic|label_add|label_remove|delete
      - ``value``: target id (or empty)
    """

    async def post(self, request, key):
        from issues.models import Label, Priority, Status
        from projects.models import Epic, Sprint

        project = await _aget_project(key)
        await aassert_can_edit(request.user, project)
        keys = request.POST.getlist("keys")
        action = request.POST.get("action", "")
        value = request.POST.get("value", "").strip()
        if not keys or not action:
            return HttpResponseBadRequest("keys + action requeridos")
        qs = Issue.objects.filter(project=project, key__in=keys)
        if action == "delete":
            await qs.adelete()
        elif action == "status":
            if not await Status.objects.filter(pk=value).aexists():
                return HttpResponseBadRequest("status inválido")
            await qs.aupdate(status_id=int(value))
        elif action == "assignee":
            if value:
                from django.db.models import Q as _Q

                from accounts.models import User
                ok = await User.objects.filter(pk=value).filter(
                    _Q(memberships__project=project) | _Q(led_projects=project)
                ).aexists()
                if not ok:
                    return HttpResponseBadRequest("usuario no en proyecto")
            await qs.aupdate(assignee_id=int(value) if value else None)
        elif action == "sprint":
            if value and not await Sprint.objects.filter(pk=value, project=project).aexists():
                return HttpResponseBadRequest("sprint no en proyecto")
            await qs.aupdate(sprint_id=int(value) if value else None)
        elif action == "priority":
            if not await Priority.objects.filter(pk=value).aexists():
                return HttpResponseBadRequest("priority inválido")
            await qs.aupdate(priority_id=int(value))
        elif action == "epic":
            if value and not await Epic.objects.filter(pk=value, project=project).aexists():
                return HttpResponseBadRequest("epic no en proyecto")
            await qs.aupdate(epic_id=int(value) if value else None)
        elif action == "label_add":
            if not value or not await Label.objects.filter(pk=value).aexists():
                return HttpResponseBadRequest("label inválido")
            label_id = int(value)
            through = Issue.labels.through
            issue_ids = [pk async for pk in qs.values_list("pk", flat=True)]
            existing = {
                pk async for pk in through.objects.filter(
                    issue_id__in=issue_ids, label_id=label_id,
                ).values_list("issue_id", flat=True)
            }
            await through.objects.abulk_create(
                [through(issue_id=pk, label_id=label_id) for pk in issue_ids if pk not in existing]
            )
        elif action == "label_remove":
            if not value or not await Label.objects.filter(pk=value).aexists():
                return HttpResponseBadRequest("label inválido")
            label_id = int(value)
            through = Issue.labels.through
            issue_ids = [pk async for pk in qs.values_list("pk", flat=True)]
            await through.objects.filter(
                issue_id__in=issue_ids, label_id=label_id,
            ).adelete()
        else:
            return HttpResponseBadRequest("action desconocida")
        return HttpResponse(status=204, headers={"HX-Redirect": f"/board/{project.key}/"})


class BacklogView(AsyncLoginRequiredMixin, AsyncTemplateView):
    """Backlog view: shows issues per sprint plus an unassigned section."""

    template_name = "board/backlog.html"

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        project = await _aget_project(self.kwargs["key"])
        await aassert_can_view(self.request.user, project)
        sprints = [s async for s in project.sprints.exclude(status="closed")]
        sprint_ids = [s.pk for s in sprints]
        # Single query for all sprint issues; group in memory to avoid N+1.
        sprint_issues = [
            i async for i in
            project.issues.filter(sprint_id__in=sprint_ids)
            .select_related("status", "priority", "issue_type", "assignee")
            .order_by("rank", "-updated_at")
        ]
        by_sprint: dict[int, list] = {sid: [] for sid in sprint_ids}
        for i in sprint_issues:
            by_sprint[i.sprint_id].append(i)
        groups = [{"sprint": s, "issues": by_sprint[s.pk]} for s in sprints]
        unassigned = [i async for i in project.issues.filter(sprint__isnull=True).exclude(status__category="done").select_related("status", "priority", "issue_type", "assignee").order_by("-updated_at")]
        groups.append({"sprint": None, "issues": unassigned})
        ctx["project"] = project
        ctx["groups"] = groups
        ctx["sprints"] = sprints
        return ctx


class BoardColumnQuickCreateView(AsyncLoginRequiredMixin, View):
    """Create a new issue directly in a board column (sticky-note style).

    Receives ``summary`` + ``status_id``; returns the rendered card so the
    board can append it without a full reload.
    """

    async def post(self, request, key):
        from issues.models import IssueType, Priority, Status
        project = await _aget_project(key)
        await aassert_can_edit(request.user, project)
        summary = request.POST.get("summary", "").strip()
        status_id = request.POST.get("status_id", "")
        if not summary or not status_id.isdigit():
            return HttpResponseBadRequest("summary + status requeridos")
        status = await Status.objects.filter(pk=int(status_id)).afirst()
        if status is None:
            return HttpResponseBadRequest("status inválido")
        priority = await Priority.objects.afirst()
        itype = await IssueType.objects.afirst()
        num = await project.anext_issue_number()
        issue = Issue(
            project=project, reporter=request.user,
            summary=summary[:255], description="",
            status=status, priority=priority, issue_type=itype,
            key=f"{project.key}-{num}",
        )
        await issue.asave()
        return await arender(request, "board/_card.html", {"issue": issue})


class BoardViewListView(AsyncLoginRequiredMixin, View):
    """Return the saved-view picker fragment for a project."""

    async def get(self, request, key):
        from .models import SavedBoardView
        project = await _aget_project(key)
        await aassert_can_view(request.user, project)
        views = [
            v async for v in
            SavedBoardView.objects.filter(user=request.user, project=project)
        ]
        return await arender(
            request, "board/_view_picker.html",
            {"project": project, "views": views, "current": request.GET.urlencode()},
        )


class BoardViewSaveView(AsyncLoginRequiredMixin, View):
    """Persist the current filter combination as a named view."""

    async def post(self, request, key):
        from .models import SavedBoardView
        project = await _aget_project(key)
        await aassert_can_view(request.user, project)
        name = request.POST.get("name", "").strip()[:80]
        if not name:
            return HttpResponseBadRequest("nombre requerido")
        # The set of params we know about — anything else is ignored to keep
        # links predictable when the schema evolves.
        keys = (
            "assignee", "type", "priority", "epic", "sprint",
            "stale", "due", "text", "urgent", "review", "recent",
        )
        filters = {k: request.POST.get(k, "") for k in keys if request.POST.get(k)}
        await SavedBoardView.objects.aupdate_or_create(
            user=request.user, project=project, name=name,
            defaults={"filters": filters},
        )
        return HttpResponse(status=204, headers={"HX-Redirect": f"/board/{project.key}/?{request.POST.urlencode()}"})


class BoardViewDeleteView(AsyncLoginRequiredMixin, View):
    async def post(self, request, key, pk):
        from .models import SavedBoardView
        await SavedBoardView.objects.filter(pk=pk, user=request.user).adelete()
        return redirect("board:board", key=key)


class BacklogMoveView(AsyncLoginRequiredMixin, View):
    """Move an issue to a given sprint (or backlog if ``sprint`` is empty)."""

    async def post(self, request, key):
        try:
            issue = await Issue.objects.select_related("project").aget(key=key)
        except Issue.DoesNotExist as exc:
            raise Http404 from exc
        await aassert_can_edit(request.user, issue.project)
        sprint_id = request.POST.get("sprint", "").strip()
        if sprint_id:
            from projects.models import Sprint
            issue.sprint = await Sprint.objects.aget(pk=int(sprint_id))
        else:
            issue.sprint = None
        await issue.asave()
        return await arender(request, "board/_backlog_row.html", {"i": issue})
