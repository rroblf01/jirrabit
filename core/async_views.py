"""Async-capable generic view bases.

Django's built-in generic views (``TemplateView``, ``FormView``, ``ListView``,
``DetailView``, ``CreateView``, ``UpdateView``) have sync ``get``/``post``
handlers and call ``get_context_data``, ``get_object``, ``get_queryset`` and
``form_valid``/``form_invalid`` synchronously. We can't just swap them in an
ASGI/daphne stack because any DB access inside those hooks raises
``SynchronousOnlyOperation``.

The classes here keep the same public surface (``template_name``,
``form_class``, ``model``, ``slug_field``, ``success_url``, etc.) so
subclasses still feel like writing a Django generic view, but the lifecycle
runs on the event loop. The hooks subclasses override are named
``aget_*``/``aform_*``/``aget_context_data``. Defaults call the sync
counterparts for backward compatibility when no DB is involved.
"""
from django.shortcuts import redirect
from django.views.generic import (
    CreateView,
    DetailView,
    FormView,
    ListView,
    TemplateView,
    UpdateView,
)

from .aio import arender, avalid


class _AsyncContextMixin:
    """Provides an async ``aget_context_data`` that subclasses override."""

    async def aget_context_data(self, **kwargs):
        return super().get_context_data(**kwargs)


class AsyncTemplateView(_AsyncContextMixin, TemplateView):
    async def get(self, request, *args, **kwargs):
        context = await self.aget_context_data(**kwargs)
        return await arender(request, self.get_template_names(), context)


class _AsyncFormMixin(_AsyncContextMixin):
    async def aget_form(self, form_class=None):
        """Construct the form. Override when DB is touched in ``__init__``
        (e.g. ``ModelForm(instance=...)`` with M2M fields) and wrap the
        construction with :func:`core.aio.aform`."""
        if form_class is None:
            form_class = self.get_form_class()
        return form_class(**self.get_form_kwargs())

    async def aform_valid(self, form):
        return redirect(await self.aget_success_url())

    async def aform_invalid(self, form):
        context = await self.aget_context_data(form=form)
        return await arender(self.request, self.get_template_names(), context)

    async def aget_success_url(self):
        return self.get_success_url()


class AsyncFormView(_AsyncFormMixin, FormView):
    async def get(self, request, *args, **kwargs):
        form = await self.aget_form()
        context = await self.aget_context_data(form=form)
        return await arender(request, self.get_template_names(), context)

    async def post(self, request, *args, **kwargs):
        form = await self.aget_form()
        if await avalid(form):
            return await self.aform_valid(form)
        return await self.aform_invalid(form)

    # ``ProcessFormView`` aliases ``put = post`` (sync); re-alias to the
    # async override so the View's all-handlers-same-flavor check passes.
    put = post


class _AsyncObjectMixin:
    """Async equivalent of ``SingleObjectMixin.get_object``."""

    async def aget_object(self):
        return self.get_object()


class AsyncDetailView(_AsyncContextMixin, _AsyncObjectMixin, DetailView):
    async def get(self, request, *args, **kwargs):
        self.object = await self.aget_object()
        context_kwargs = {self.context_object_name or "object": self.object}
        context = await self.aget_context_data(**context_kwargs)
        return await arender(request, self.get_template_names(), context)


class AsyncListView(_AsyncContextMixin, ListView):
    async def aget_queryset(self):
        return self.get_queryset()

    async def get(self, request, *args, **kwargs):
        qs = await self.aget_queryset()
        self.object_list = [obj async for obj in qs]
        context_kwargs = {self.context_object_name or "object_list": self.object_list}
        context = await self.aget_context_data(**context_kwargs)
        return await arender(request, self.get_template_names(), context)


class AsyncCreateView(_AsyncFormMixin, CreateView):
    async def get(self, request, *args, **kwargs):
        self.object = None
        form = await self.aget_form()
        context = await self.aget_context_data(form=form)
        return await arender(request, self.get_template_names(), context)

    async def post(self, request, *args, **kwargs):
        self.object = None
        form = await self.aget_form()
        if await avalid(form):
            return await self.aform_valid(form)
        return await self.aform_invalid(form)

    async def aform_valid(self, form):
        instance = form.save(commit=False)
        await instance.asave()
        self.object = instance
        # M2M fields require an explicit aset after asave; subclasses
        # that need that should override and call this base first.
        return redirect(await self.aget_success_url())

    put = post


class AsyncUpdateView(_AsyncFormMixin, _AsyncObjectMixin, UpdateView):
    async def get(self, request, *args, **kwargs):
        self.object = await self.aget_object()
        form = await self.aget_form()
        context = await self.aget_context_data(form=form)
        return await arender(request, self.get_template_names(), context)

    async def post(self, request, *args, **kwargs):
        self.object = await self.aget_object()
        form = await self.aget_form()
        if await avalid(form):
            return await self.aform_valid(form)
        return await self.aform_invalid(form)

    async def aform_valid(self, form):
        instance = form.save(commit=False)
        await instance.asave()
        self.object = instance
        return redirect(await self.aget_success_url())

    put = post
