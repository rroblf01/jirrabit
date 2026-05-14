"""Admin-only user management.

Requires the requesting user to be a Django superuser. Distinct from
project-scoped roles (those live in ``projects.ProjectMembership``).
"""
from django.contrib.auth.hashers import make_password
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views import View

from core.aio import arender, avalid
from core.async_views import AsyncListView
from core.mixins import AsyncLoginRequiredMixin

from .forms import RegisterForm
from .models import User


class AsyncSuperuserRequiredMixin(AsyncLoginRequiredMixin):
    async def dispatch(self, request, *args, **kwargs):
        user = await request.auser()
        if not (user.is_authenticated and user.is_superuser):
            raise PermissionDenied("Solo administradores.")
        request.user = user
        # bypass AsyncLoginRequiredMixin (already validated)
        from django.views import View
        return await View.dispatch(self, request, *args, **kwargs)


class AdminUserListView(AsyncSuperuserRequiredMixin, AsyncListView):
    template_name = "accounts/admin/list.html"
    context_object_name = "users"

    async def aget_queryset(self):
        qs = User.objects.all().order_by("username")
        q = self.request.GET.get("q", "").strip()
        if q:
            from django.db.models import Q
            qs = qs.filter(Q(username__icontains=q) | Q(email__icontains=q) | Q(display_name__icontains=q))
        return qs

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        ctx["q"] = self.request.GET.get("q", "")
        return ctx


class AdminUserCreateView(AsyncSuperuserRequiredMixin, View):
    template_name = "accounts/admin/edit.html"

    async def get(self, request):
        return await arender(request, self.template_name, {"form": RegisterForm(), "create": True})

    async def post(self, request):
        form = RegisterForm(request.POST)
        if not await avalid(form):
            return await arender(request, self.template_name, {"form": form, "create": True})
        user = form.save(commit=False)
        await user.asave()
        return redirect("accounts:admin_user_list")


class AdminUserEditView(AsyncSuperuserRequiredMixin, View):
    template_name = "accounts/admin/edit.html"

    async def _get_user(self, pk):
        try:
            return await User.objects.aget(pk=pk)
        except User.DoesNotExist as exc:
            raise Http404 from exc

    async def get(self, request, pk):
        u = await self._get_user(pk)
        return await arender(request, self.template_name, {"u": u, "create": False})

    async def post(self, request, pk):
        u = await self._get_user(pk)
        u.email = request.POST.get("email", u.email).strip()
        u.display_name = request.POST.get("display_name", u.display_name).strip()
        u.job_title = request.POST.get("job_title", u.job_title).strip()
        u.is_active = bool(request.POST.get("is_active"))
        u.is_staff = bool(request.POST.get("is_staff"))
        u.is_superuser = bool(request.POST.get("is_superuser"))
        new_pw = request.POST.get("new_password", "").strip()
        if new_pw:
            u.password = make_password(new_pw)
        await u.asave()
        return redirect("accounts:admin_user_list")


class AdminUserToggleActiveView(AsyncSuperuserRequiredMixin, View):
    async def post(self, request, pk):
        try:
            u = await User.objects.aget(pk=pk)
        except User.DoesNotExist as exc:
            raise Http404 from exc
        u.is_active = not u.is_active
        await u.asave()
        return await arender(request, "accounts/admin/_row.html", {"u": u})


# --- invite tokens ---

import secrets
from datetime import timedelta

from django.utils import timezone

from .models import InviteToken


class AdminInviteListView(AsyncSuperuserRequiredMixin, AsyncListView):
    template_name = "accounts/admin/invites.html"
    context_object_name = "invites"

    async def aget_queryset(self):
        return InviteToken.objects.all().select_related("created_by", "used_by")


class AdminInviteCreateView(AsyncSuperuserRequiredMixin, View):
    async def post(self, request):
        days = int(request.POST.get("days", "7"))
        invite = await InviteToken.objects.acreate(
            created_by=request.user,
            email=request.POST.get("email", "").strip(),
            role=request.POST.get("role", "member"),
            token=secrets.token_urlsafe(32),
            expires_at=timezone.now() + timedelta(days=days),
        )
        url = request.build_absolute_uri(f"/accounts/register/?token={invite.token}")
        return await arender(
            request, "accounts/admin/_invite_row.html",
            {"i": invite, "url": url, "fresh": True},
        )


class AdminInviteRevokeView(AsyncSuperuserRequiredMixin, View):
    async def post(self, request, pk):
        i = await InviteToken.objects.filter(pk=pk).afirst()
        if i and not i.used_at:
            i.expires_at = timezone.now()
            await i.asave(update_fields=["expires_at"])
        if i:
            return await arender(request, "accounts/admin/_invite_row.html", {"i": i})
        from django.http import HttpResponse
        return HttpResponse("")
