from .palettes import palette_css


def nav(request):
    """Expose the nav-project list pre-loaded by ``nav_context_middleware``."""
    return {"nav_projects": getattr(request, "nav_projects", [])}


def palette(request):
    """Inject the user's color palette CSS into every template.

    Anonymous users get the default ``blue`` palette. The actual overrides
    live in :mod:`core.palettes`.
    """
    slug = "blue"
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        slug = getattr(user, "palette", "blue") or "blue"
    return {"palette_slug": slug, "palette_css": palette_css(slug)}


def notifications_count(request):
    """Expose unread notification count for the topbar badge.

    Populated by ``core.middleware.nav_context_middleware`` (which
    already runs async) on ``request.unread_notifications``.
    """
    return {"unread_notifications": getattr(request, "unread_notifications", 0)}
