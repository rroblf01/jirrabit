"""Custom middlewares.

- ``nav_context_middleware`` pre-loads the sidebar's project list so the
  context processor stays DB-free in async views.
- ``login_throttle_middleware`` blocks brute-force attempts against
  ``/accounts/login/`` by IP+username. In-memory counters, reset on
  process restart. For multi-instance deploys swap the cache for Redis.
"""

import time
from collections import defaultdict
from inspect import iscoroutinefunction
from threading import Lock

from django.conf import settings
from django.http import HttpResponse
from django.utils.decorators import sync_and_async_middleware

# ---------------------------------------------------------------------------
# Per-user language
# ---------------------------------------------------------------------------


@sync_and_async_middleware
def user_language_middleware(get_response):
    """Activate the authenticated user's preferred ``language``.

    Runs after Django's ``LocaleMiddleware`` so the per-user setting
    overrides whatever the Accept-Language header would have selected.
    Always deactivates after the response so the per-thread translation
    state does not bleed across requests (e.g. user A's language
    sticking to user B's response inside the same daphne worker).
    """
    from django.utils import translation

    def _apply(user, request) -> bool:
        lang = getattr(user, "language", None) if user and user.is_authenticated else None
        if not lang:
            return False
        translation.activate(lang)
        request.LANGUAGE_CODE = lang
        return True

    if iscoroutinefunction(get_response):

        async def middleware(request):
            user = await request.auser()
            activated = _apply(user, request)
            try:
                return await get_response(request)
            finally:
                if activated:
                    translation.deactivate()

        return middleware

    def middleware(request):
        activated = _apply(getattr(request, "user", None), request)
        try:
            return get_response(request)
        finally:
            if activated:
                translation.deactivate()

    return middleware


# ---------------------------------------------------------------------------
# Nav projects
# ---------------------------------------------------------------------------


@sync_and_async_middleware
def nav_context_middleware(get_response):
    from projects.models import Project

    if iscoroutinefunction(get_response):

        async def middleware(request):
            user = await request.auser()
            if user.is_authenticated:
                request.nav_projects = [p async for p in Project.objects.filter_visible(user)[:8]]
                # ``unread_count`` is a denormalised field on User maintained
                # by accounts.signals — no extra COUNT(*) per request.
                request.unread_notifications = getattr(user, "unread_count", 0)
            else:
                request.nav_projects = []
                request.unread_notifications = 0
            return await get_response(request)

        return middleware

    def middleware(request):
        if request.user.is_authenticated:
            request.nav_projects = list(Project.objects.filter_visible(request.user)[:8])
            request.unread_notifications = getattr(request.user, "unread_count", 0)
        else:
            request.nav_projects = []
            request.unread_notifications = 0
        return get_response(request)

    return middleware


# ---------------------------------------------------------------------------
# Login throttle
# ---------------------------------------------------------------------------

# Map of bucket key -> list of attempt timestamps and ban-until epoch seconds.
_attempts: dict[str, list[float]] = defaultdict(list)
_banned_until: dict[str, float] = {}
_lock = Lock()


def _client_ip(request) -> str:
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "0.0.0.0")


def _bucket_keys(request) -> list[str]:
    ip = _client_ip(request)
    username = (request.POST.get("username") or "").strip().lower()
    keys = [f"ip:{ip}"]
    if username:
        keys.append(f"u:{username}")
    return keys


def _check_throttled(request) -> str | None:
    now = time.monotonic()
    window = settings.JIRRABIT_LOGIN_THROTTLE_WINDOW
    limit = settings.JIRRABIT_LOGIN_THROTTLE_LIMIT
    ban_seconds = settings.JIRRABIT_LOGIN_THROTTLE_BAN
    with _lock:
        for key in _bucket_keys(request):
            until = _banned_until.get(key, 0)
            if until and until > now:
                return key
            attempts = _attempts[key]
            # drop stale entries
            _attempts[key] = [t for t in attempts if now - t < window]
            if len(_attempts[key]) >= limit:
                _banned_until[key] = now + ban_seconds
                return key
    return None


def _record_failure(request) -> None:
    now = time.monotonic()
    with _lock:
        for key in _bucket_keys(request):
            _attempts[key].append(now)


def _clear(request) -> None:
    with _lock:
        for key in _bucket_keys(request):
            _attempts.pop(key, None)
            _banned_until.pop(key, None)


@sync_and_async_middleware
def login_throttle_middleware(get_response):
    """Block excessive login attempts.

    Counts failures per IP and per username over a sliding window. When the
    limit is reached the offender is banned for ``JIRRABIT_LOGIN_THROTTLE_BAN``
    seconds and any further POST to ``/accounts/login/`` returns 429.
    """

    login_path = "/accounts/login/"

    def _is_login_post(request) -> bool:
        return request.method == "POST" and request.path == login_path

    def _too_many(banned_key: str) -> HttpResponse:
        return HttpResponse(
            f"Demasiados intentos. Espera unos minutos. ({banned_key})",
            status=429,
            headers={"Retry-After": str(settings.JIRRABIT_LOGIN_THROTTLE_BAN)},
        )

    if iscoroutinefunction(get_response):

        async def middleware(request):
            if _is_login_post(request):
                banned = _check_throttled(request)
                if banned:
                    return _too_many(banned)
                response = await get_response(request)
                # 302 redirect with location != /accounts/login/ means success.
                if response.status_code == 302 and not response["Location"].endswith(login_path):
                    _clear(request)
                else:
                    _record_failure(request)
                return response
            return await get_response(request)

        return middleware

    def middleware(request):
        if _is_login_post(request):
            banned = _check_throttled(request)
            if banned:
                return _too_many(banned)
            response = get_response(request)
            if response.status_code == 302 and not response["Location"].endswith(login_path):
                _clear(request)
            else:
                _record_failure(request)
            return response
        return get_response(request)

    return middleware


# ---------------------------------------------------------------------------
# Content-Security-Policy header
# ---------------------------------------------------------------------------


@sync_and_async_middleware
def csp_middleware(get_response):
    """Set a strict ``Content-Security-Policy`` on every response.

    ``'unsafe-inline'`` on ``style-src`` and ``script-src`` is required
    because the project still ships some inline styles/scripts (palette
    overrides, board drag-and-drop bootstrap, htmx CDN tag). ``'unsafe-eval'``
    is required by htmx 2.x + idiomorph extension, which use ``Function()``
    to evaluate expressions in attributes like ``hx-vals`` and the morph
    extension's swap logic. Tighten once we move scripts to external
    files and audit htmx attribute usage.
    """
    policy = (
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://unpkg.com; "
        "connect-src 'self' ws: wss:; "
        "font-src 'self' data:; "
        # ``frame-src data:`` is required to let the PDF attachment
        # preview iframe load from a ``data:application/pdf`` URL.
        "frame-src 'self' data:; "
        "frame-ancestors 'none'; "
        "form-action 'self'; "
        "base-uri 'self'; "
        "object-src 'none';"
    )

    def _apply(response):
        response.setdefault("Content-Security-Policy", policy)
        response.setdefault("X-Content-Type-Options", "nosniff")
        return response

    if iscoroutinefunction(get_response):

        async def middleware(request):
            return _apply(await get_response(request))

        return middleware

    def middleware(request):
        return _apply(get_response(request))

    return middleware


# ---------------------------------------------------------------------------
# API rate limit
# ---------------------------------------------------------------------------

_api_attempts: dict[str, list[float]] = defaultdict(list)


def _api_bucket(request, user=None) -> str | None:
    """Return the bucket key for an API request.

    Prefers the bearer token (cheap, no DB hit) so we don't have to resolve
    the session user; falls back to the resolved user when present, or to
    the client IP for anonymous calls.
    """
    if not request.path.startswith("/api/"):
        return None
    auth = request.META.get("HTTP_AUTHORIZATION", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        return f"apikey:{token[:8]}"
    if user is not None and getattr(user, "is_authenticated", False):
        return f"session:{user.pk}"
    return f"ip:{_client_ip(request)}"


def _api_throttled(key: str) -> bool:
    now = time.monotonic()
    window = settings.JIRRABIT_API_RATE_WINDOW
    limit = settings.JIRRABIT_API_RATE_LIMIT
    with _lock:
        attempts = [t for t in _api_attempts[key] if now - t < window]
        if len(attempts) >= limit:
            _api_attempts[key] = attempts
            return True
        attempts.append(now)
        _api_attempts[key] = attempts
        return False


@sync_and_async_middleware
def api_rate_limit_middleware(get_response):
    """Throttle ``/api/*`` per identity (API key prefix, session pk or IP).

    Limit configurable via ``JIRRABIT_API_RATE_LIMIT`` (default 120) over
    ``JIRRABIT_API_RATE_WINDOW`` seconds (default 60). Replies 429 with a
    ``Retry-After`` header when the bucket is exhausted.
    """

    def _too_many() -> HttpResponse:
        from django.utils.translation import gettext as _

        retry = settings.JIRRABIT_API_RATE_WINDOW
        body = _("Rate limit exceeded. Retry in %(s)s seconds.") % {"s": retry}
        return HttpResponse(
            body,
            status=429,
            content_type="text/plain; charset=utf-8",
            headers={"Retry-After": str(retry)},
        )

    if iscoroutinefunction(get_response):

        async def middleware(request):
            if request.path.startswith("/api/"):
                auth = request.META.get("HTTP_AUTHORIZATION", "")
                user = None
                if not auth.lower().startswith("bearer "):
                    # Cookie auth — resolve via auser() to avoid the lazy
                    # ``request.user`` access raising SynchronousOnlyOperation.
                    user = await request.auser()
                key = _api_bucket(request, user=user)
                if key and _api_throttled(key):
                    return _too_many()
            return await get_response(request)

        return middleware

    def middleware(request):
        if request.path.startswith("/api/"):
            key = _api_bucket(request, user=request.user)
            if key and _api_throttled(key):
                return _too_many()
        return get_response(request)

    return middleware
