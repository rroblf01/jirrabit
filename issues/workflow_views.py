"""Global workflow editor views.

CRUD over the four global tables that shape every project's issue lifecycle:

- :class:`issues.models.Status` (with transitions through ``allowed_next``)
- :class:`issues.models.Priority`
- :class:`issues.models.IssueType`
- :class:`issues.models.Label`

Access is restricted to superusers via :class:`core.mixins.AsyncSuperuserRequiredMixin`.
Deletion of an entity that's still referenced by issues is refused (the FK is
``PROTECT``-bound), so the views surface a friendly error instead of letting
``IntegrityError`` bubble up.
"""
from django.db.models import ProtectedError
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views import View

from asgiref.sync import sync_to_async

from core.aio import aform, arender, avalid
from core.async_views import (
    AsyncCreateView,
    AsyncListView,
    AsyncTemplateView,
    AsyncUpdateView,
)
from core.mixins import AsyncSuperuserRequiredMixin

from .models import IssueType, Label, Priority, Status
from .workflow_forms import (
    IssueTypeForm,
    LabelForm,
    PriorityForm,
    StatusForm,
    StatusTransitionsForm,
)


# --- overview ----------------------------------------------------------------

class WorkflowOverviewView(AsyncSuperuserRequiredMixin, AsyncTemplateView):
    template_name = "workflow/overview.html"

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        ctx["status_count"] = await Status.objects.acount()
        ctx["priority_count"] = await Priority.objects.acount()
        ctx["type_count"] = await IssueType.objects.acount()
        ctx["label_count"] = await Label.objects.acount()
        return ctx


# --- helpers -----------------------------------------------------------------

async def _aprotect_delete(obj):
    """Run ``obj.delete()`` and return ``True`` if the row was deleted,
    ``False`` if it's still referenced (PROTECT)."""
    try:
        await sync_to_async(obj.delete, thread_sensitive=True)()
        return True
    except ProtectedError:
        return False


# --- Status ------------------------------------------------------------------

class StatusListView(AsyncSuperuserRequiredMixin, AsyncListView):
    template_name = "workflow/status_list.html"
    context_object_name = "statuses"

    async def aget_queryset(self):
        return Status.objects.order_by("order", "id").prefetch_related("allowed_next")

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        for s in ctx["statuses"]:
            s.usage_count = await s.issues.acount()
        return ctx


class StatusCreateView(AsyncSuperuserRequiredMixin, AsyncCreateView):
    form_class = StatusForm
    template_name = "workflow/status_form.html"
    success_url = reverse_lazy("workflow:status_list")


class StatusUpdateView(AsyncSuperuserRequiredMixin, AsyncUpdateView):
    model = Status
    form_class = StatusForm
    template_name = "workflow/status_form.html"
    success_url = reverse_lazy("workflow:status_list")

    async def aget_object(self):
        return await Status.objects.aget(pk=self.kwargs["pk"])


class StatusDeleteView(AsyncSuperuserRequiredMixin, View):
    async def post(self, request, pk):
        try:
            status = await Status.objects.aget(pk=pk)
        except Status.DoesNotExist:
            return HttpResponseBadRequest("status no existe")
        if not await _aprotect_delete(status):
            from django.contrib import messages
            messages.error(
                request,
                f"No se puede borrar «{status.name}»: aún tiene tareas referenciándolo.",
            )
        return redirect("workflow:status_list")


class StatusTransitionsView(AsyncSuperuserRequiredMixin, View):
    """Render and persist the ``allowed_next`` checkbox set for a Status."""

    template_name = "workflow/transitions_form.html"

    async def _aget_status(self, pk):
        from django.http import Http404
        try:
            return await Status.objects.aget(pk=pk)
        except Status.DoesNotExist as exc:
            raise Http404 from exc

    async def get(self, request, pk):
        status = await self._aget_status(pk)
        form = await aform(StatusTransitionsForm, instance=status)
        all_statuses = [s async for s in Status.objects.exclude(pk=pk).order_by("order")]
        current_ids = {
            s.pk async for s in status.allowed_next.all()
        }
        return await arender(
            request, self.template_name,
            {
                "status": status, "form": form,
                "all_statuses": all_statuses, "current_ids": current_ids,
            },
        )

    async def post(self, request, pk):
        status = await self._aget_status(pk)
        # Bypass aform: M2M-only update, validate ids server-side.
        ids_raw = request.POST.getlist("allowed_next")
        try:
            ids = [int(i) for i in ids_raw]
        except ValueError:
            return HttpResponseBadRequest("ids inválidos")
        if pk in ids:
            return HttpResponseBadRequest("un estado no puede transicionar a sí mismo")
        valid_ids = {
            sid async for sid in
            Status.objects.exclude(pk=pk).filter(pk__in=ids).values_list("pk", flat=True)
        }
        await status.allowed_next.aset(valid_ids)
        from django.contrib import messages
        messages.success(request, f"Transiciones de «{status.name}» actualizadas.")
        return redirect("workflow:status_list")


class StatusTransitionsMatrixView(AsyncSuperuserRequiredMixin, View):
    """Full transitions matrix: rows = source status, cols = target status.

    GET renders the matrix as a grid of checkboxes; POST saves the entire
    matrix in one operation. Useful when defining a brand-new workflow.
    """

    async def get(self, request):
        import json
        statuses = [s async for s in Status.objects.order_by("order", "id")]
        # Pre-compute allowed sets per status to avoid N+1 in the template.
        allowed = {}
        for s in statuses:
            allowed[s.pk] = [t.pk async for t in s.allowed_next.all()]
        return await arender(
            request, "workflow/matrix.html",
            {
                "statuses": statuses,
                "allowed": allowed,
                "allowed_json": json.dumps(allowed),
            },
        )

    async def post(self, request):
        statuses = [s async for s in Status.objects.order_by("order", "id")]
        status_pks = {s.pk for s in statuses}
        for s in statuses:
            raw = request.POST.getlist(f"allowed_{s.pk}")
            try:
                targets = {int(t) for t in raw} & status_pks
            except ValueError:
                continue
            targets.discard(s.pk)
            await s.allowed_next.aset(targets)
        from django.contrib import messages
        messages.success(request, "Matriz de transiciones guardada.")
        return redirect("workflow:matrix")


# --- Priority ----------------------------------------------------------------

class PriorityListView(AsyncSuperuserRequiredMixin, AsyncListView):
    template_name = "workflow/priority_list.html"
    context_object_name = "priorities"

    async def aget_queryset(self):
        return Priority.objects.order_by("-weight", "name")

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        for p in ctx["priorities"]:
            p.usage_count = await p.issues.acount()
        return ctx


class PriorityCreateView(AsyncSuperuserRequiredMixin, AsyncCreateView):
    form_class = PriorityForm
    template_name = "workflow/priority_form.html"
    success_url = reverse_lazy("workflow:priority_list")


class PriorityUpdateView(AsyncSuperuserRequiredMixin, AsyncUpdateView):
    model = Priority
    form_class = PriorityForm
    template_name = "workflow/priority_form.html"
    success_url = reverse_lazy("workflow:priority_list")

    async def aget_object(self):
        return await Priority.objects.aget(pk=self.kwargs["pk"])


class PriorityDeleteView(AsyncSuperuserRequiredMixin, View):
    async def post(self, request, pk):
        try:
            p = await Priority.objects.aget(pk=pk)
        except Priority.DoesNotExist:
            return HttpResponseBadRequest("priority no existe")
        if not await _aprotect_delete(p):
            from django.contrib import messages
            messages.error(
                request,
                f"No se puede borrar «{p.name}»: hay tareas usándola.",
            )
        return redirect("workflow:priority_list")


# --- IssueType ---------------------------------------------------------------

class IssueTypeListView(AsyncSuperuserRequiredMixin, AsyncListView):
    template_name = "workflow/type_list.html"
    context_object_name = "types"

    async def aget_queryset(self):
        return IssueType.objects.order_by("name")

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        for t in ctx["types"]:
            t.usage_count = await t.issues.acount()
        return ctx


class IssueTypeCreateView(AsyncSuperuserRequiredMixin, AsyncCreateView):
    form_class = IssueTypeForm
    template_name = "workflow/type_form.html"
    success_url = reverse_lazy("workflow:type_list")


class IssueTypeUpdateView(AsyncSuperuserRequiredMixin, AsyncUpdateView):
    model = IssueType
    form_class = IssueTypeForm
    template_name = "workflow/type_form.html"
    success_url = reverse_lazy("workflow:type_list")

    async def aget_object(self):
        return await IssueType.objects.aget(pk=self.kwargs["pk"])


class IssueTypeDeleteView(AsyncSuperuserRequiredMixin, View):
    async def post(self, request, pk):
        try:
            t = await IssueType.objects.aget(pk=pk)
        except IssueType.DoesNotExist:
            return HttpResponseBadRequest("type no existe")
        if not await _aprotect_delete(t):
            from django.contrib import messages
            messages.error(
                request,
                f"No se puede borrar «{t.name}»: hay tareas usándolo.",
            )
        return redirect("workflow:type_list")


# --- Label -------------------------------------------------------------------

class LabelListView(AsyncSuperuserRequiredMixin, AsyncListView):
    template_name = "workflow/label_list.html"
    context_object_name = "labels"

    async def aget_queryset(self):
        return Label.objects.order_by("name")

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        for lab in ctx["labels"]:
            lab.usage_count = await lab.issues.acount()
        return ctx


class LabelCreateView(AsyncSuperuserRequiredMixin, AsyncCreateView):
    form_class = LabelForm
    template_name = "workflow/label_form.html"
    success_url = reverse_lazy("workflow:label_list")


class LabelUpdateView(AsyncSuperuserRequiredMixin, AsyncUpdateView):
    model = Label
    form_class = LabelForm
    template_name = "workflow/label_form.html"
    success_url = reverse_lazy("workflow:label_list")

    async def aget_object(self):
        return await Label.objects.aget(pk=self.kwargs["pk"])


class LabelDeleteView(AsyncSuperuserRequiredMixin, View):
    async def post(self, request, pk):
        try:
            lab = await Label.objects.aget(pk=pk)
        except Label.DoesNotExist:
            return HttpResponseBadRequest("label no existe")
        # Label uses M2M on Issue.labels (no PROTECT), so it always succeeds.
        await sync_to_async(lab.delete, thread_sensitive=True)()
        return redirect("workflow:label_list")


# --- Reorder (Status) --------------------------------------------------------

class StatusReorderView(AsyncSuperuserRequiredMixin, View):
    """Persist a new ``order`` for each status from a single POST.

    Expects ``order[]=pk`` lists in the order desired (rendered by the JS
    sortable layer).
    """

    async def post(self, request):
        ids = request.POST.getlist("order[]") or request.POST.getlist("order")
        if not ids:
            return HttpResponseBadRequest("orden vacío")
        try:
            ordered_pks = [int(i) for i in ids]
        except ValueError:
            return HttpResponseBadRequest("ids inválidos")
        # Run updates in a single transaction for atomicity.
        def _apply():
            from django.db import transaction
            with transaction.atomic():
                for idx, pk in enumerate(ordered_pks):
                    Status.objects.filter(pk=pk).update(order=idx)
        await sync_to_async(_apply, thread_sensitive=True)()
        return redirect("workflow:status_list")
