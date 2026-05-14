from django.conf import settings
from django.contrib.auth import aauthenticate, alogin, alogout
from django.contrib.auth.forms import AuthenticationForm
from django.db.models import Q
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views import View

from core.aio import arender
from core.async_views import AsyncFormView, AsyncListView, AsyncUpdateView
from core.mixins import AsyncLoginRequiredMixin

from .forms import ProfileForm, RegisterForm
from .models import Notification, User


class JirrabitLoginView(AsyncFormView):
    form_class = AuthenticationForm
    template_name = "accounts/login.html"
    success_url = reverse_lazy("core:home")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        # AuthenticationForm wants ``request`` as positional arg.
        kwargs.pop("initial", None)
        return kwargs

    async def aget_form(self, form_class=None):
        return AuthenticationForm(self.request, data=self.request.POST or None)

    async def aform_valid(self, form):
        username = self.request.POST.get("username", "").strip()
        password = self.request.POST.get("password", "")
        user = await aauthenticate(self.request, username=username, password=password)
        if user is None:
            form.add_error(None, "Credenciales inválidas.")
            return await self.aform_invalid(form)
        await alogin(self.request, user)
        next_url = (
            self.request.POST.get("next")
            or self.request.GET.get("next")
            or settings.LOGIN_REDIRECT_URL
        )
        return redirect(next_url)


class JirrabitLogoutView(View):
    async def get(self, request):
        return await self.post(request)

    async def post(self, request):
        await alogout(request)
        return redirect(settings.LOGOUT_REDIRECT_URL)


class RegisterView(AsyncFormView):
    form_class = RegisterForm
    template_name = "accounts/register.html"
    success_url = reverse_lazy("core:home")

    async def _aget_valid_invite(self):
        """Return a valid ``InviteToken`` for the current request, or ``None``."""
        from django.conf import settings as dj_settings
        from django.utils import timezone

        from .models import InviteToken

        if not dj_settings.JIRRABIT_INVITE_ONLY:
            return "open"  # registration is open, no invite required
        token = self.request.GET.get("token") or self.request.POST.get("token") or ""
        if not token:
            return None
        return await InviteToken.objects.filter(
            token=token, used_at__isnull=True, expires_at__gt=timezone.now()
        ).afirst()

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        ctx["invite_only"] = settings.JIRRABIT_INVITE_ONLY
        ctx["token"] = self.request.GET.get("token") or self.request.POST.get("token") or ""
        ctx["invite_invalid"] = (
            settings.JIRRABIT_INVITE_ONLY and (await self._aget_valid_invite()) is None
        )
        return ctx

    async def get(self, request, *args, **kwargs):
        return await super().get(request, *args, **kwargs)

    async def post(self, request, *args, **kwargs):
        invite = await self._aget_valid_invite()
        if invite is None:
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied("Necesitas una invitación válida para registrarte.")
        self._invite = invite
        return await super().post(request, *args, **kwargs)

    async def aform_valid(self, form):
        from django.utils import timezone

        user = form.save(commit=False)
        await user.asave()
        invite = getattr(self, "_invite", None)
        if invite and invite != "open":
            invite.used_at = timezone.now()
            invite.used_by = user
            await invite.asave(update_fields=["used_at", "used_by"])
        await alogin(self.request, user)
        return redirect(await self.aget_success_url())


class ProfileView(AsyncLoginRequiredMixin, AsyncUpdateView):
    form_class = ProfileForm
    template_name = "accounts/profile.html"
    success_url = reverse_lazy("accounts:profile")

    async def aget_object(self):
        return self.request.user

    async def aget_form(self, form_class=None):
        # ProfileForm has no M2M, but instance binding is sync DB-free
        # so direct construction is safe here.
        return ProfileForm(
            self.request.POST or None,
            self.request.FILES or None,
            instance=self.object,
        )

    async def aform_valid(self, form):
        user = form.save(commit=False)
        if form.cleaned_data.get("clear_avatar"):
            user.avatar = ""
        encoded = form.encoded_avatar()
        if encoded is not None:
            user.avatar = encoded
        await user.asave()
        return redirect(await self.aget_success_url())


class UserListView(AsyncLoginRequiredMixin, AsyncListView):
    context_object_name = "users"

    def get_template_names(self):
        if self.request.htmx:
            return ["accounts/_user_list.html"]
        return ["accounts/user_list.html"]

    async def aget_queryset(self):
        qs = User.objects.all().order_by("username")
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(username__icontains=q) | Q(display_name__icontains=q) | Q(email__icontains=q)
            )
        return qs

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        ctx["q"] = self.request.GET.get("q", "")
        return ctx


class NotificationInboxView(AsyncLoginRequiredMixin, AsyncListView):
    template_name = "notifications/inbox.html"
    context_object_name = "notifications"

    def get_template_names(self):
        if self.request.htmx:
            return ["notifications/_list.html"]
        return [self.template_name]

    async def aget_queryset(self):
        return Notification.objects.filter(recipient=self.request.user).select_related("actor")[:100]


class NotificationMarkReadView(AsyncLoginRequiredMixin, View):
    async def post(self, request):
        ids = request.POST.getlist("id")
        qs = Notification.objects.filter(recipient=request.user)
        if ids:
            qs = qs.filter(pk__in=ids)
        await qs.aupdate(read=True)
        notifs = [n async for n in Notification.objects.filter(recipient=request.user).select_related("actor")[:100]]
        return await arender(request, "notifications/_list.html", {"notifications": notifs})


class UserMentionSearchView(AsyncLoginRequiredMixin, View):
    async def get(self, request):
        q = request.GET.get("q", "").strip()
        if not q:
            return await arender(request, "notifications/_mention_list.html", {"users": []})
        from django.db.models import Q
        qs = User.objects.filter(is_active=True).filter(
            Q(username__icontains=q) | Q(display_name__icontains=q)
        )[:8]
        users = [u async for u in qs]
        return await arender(request, "notifications/_mention_list.html", {"users": users})


class APIKeyListView(AsyncLoginRequiredMixin, AsyncListView):
    template_name = "accounts/api_keys.html"
    context_object_name = "keys"

    async def aget_queryset(self):
        from .models import APIKey
        return APIKey.objects.filter(owner=self.request.user)

    async def aget_context_data(self, **kwargs):
        ctx = await super().aget_context_data(**kwargs)
        # ``fresh_token`` is set after creation via session flash.
        ctx["fresh_token"] = self.request.session.pop("fresh_api_token", None)
        ctx["fresh_name"] = self.request.session.pop("fresh_api_name", None)
        return ctx


class APIKeyCreateView(AsyncLoginRequiredMixin, View):
    async def post(self, request):
        from asgiref.sync import sync_to_async
        from .models import APIKey
        name = request.POST.get("name", "").strip() or "default"
        _, plain = await sync_to_async(APIKey.create_for)(owner=request.user, name=name)
        # Stash the plaintext in the session so the next render can show it once.
        request.session["fresh_api_token"] = plain
        request.session["fresh_api_name"] = name
        return redirect("accounts:api_keys")


class APIKeyRevokeView(AsyncLoginRequiredMixin, View):
    async def post(self, request, pk):
        from django.utils import timezone
        from .models import APIKey
        k = await APIKey.objects.filter(pk=pk, owner=request.user).afirst()
        if k:
            k.revoked_at = timezone.now()
            await k.asave(update_fields=["revoked_at"])
        return redirect("accounts:api_keys")
