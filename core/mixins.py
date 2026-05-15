from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied


class AsyncLoginRequiredMixin:
    """Async equivalent of ``LoginRequiredMixin``.

    Uses ``await request.auser()`` to fetch the user without triggering
    ``SynchronousOnlyOperation`` inside an async view, and caches the
    resolved user on ``request.user`` so downstream code can access it
    safely (e.g. forms that read ``request.user.pk``).
    """

    login_url = None
    redirect_field_name = "next"

    async def dispatch(self, request, *args, **kwargs):
        user = await request.auser()
        if not user.is_authenticated:
            return redirect_to_login(
                request.get_full_path(),
                self.login_url or settings.LOGIN_URL,
                self.redirect_field_name,
            )
        request.user = user
        return await super().dispatch(request, *args, **kwargs)


class AsyncSuperuserRequiredMixin:
    """Superuser-only async views.

    Used for global configuration screens (workflow, status, priority,
    issue type, labels) that affect every project.
    """

    login_url = None
    redirect_field_name = "next"

    async def dispatch(self, request, *args, **kwargs):
        user = await request.auser()
        if not user.is_authenticated:
            return redirect_to_login(
                request.get_full_path(),
                self.login_url or settings.LOGIN_URL,
                self.redirect_field_name,
            )
        if not user.is_superuser:
            raise PermissionDenied("Requiere permisos de superusuario.")
        request.user = user
        return await super().dispatch(request, *args, **kwargs)
