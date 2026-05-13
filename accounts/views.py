from django.conf import settings
from django.contrib.auth import aauthenticate, alogin, alogout
from django.contrib.auth.forms import AuthenticationForm
from django.db.models import Q
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views import View

from core.async_views import AsyncFormView, AsyncListView, AsyncUpdateView
from core.mixins import AsyncLoginRequiredMixin

from .forms import ProfileForm, RegisterForm
from .models import User


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

    async def aform_valid(self, form):
        # ``UserCreationForm.save(commit=False)`` already sets the password.
        user = form.save(commit=False)
        await user.asave()
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
