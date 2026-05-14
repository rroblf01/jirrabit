"""Custom middlewares.

- ``nav_context_middleware`` pre-loads the sidebar's project list so the
  context processor stays DB-free in async views.
- ``login_throttle_middleware`` blocks brute-force attempts against
  ``/accounts/login/`` by IP+username. In-memory counters, reset on
  process restart. For multi-instance deploys swap the cache for Redis.
"""
import time
from collections import defaultdict
from threading import Lock

from asgiref.sync import iscoroutinefunction
from django.conf import settings
from django.http import HttpResponse
from django.utils.decorators import sync_and_async_middleware


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
                request.nav_projects = [
                    p async for p in Project.objects.filter_visible(user)[:8]
                ]
            else:
                request.nav_projects = []
            return await get_response(request)

        return middleware

    def middleware(request):
        if request.user.is_authenticated:
            request.nav_projects = list(Project.objects.filter_visible(request.user)[:8])
        else:
            request.nav_projects = []
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
