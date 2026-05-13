def nav(request):
    """Expose the nav-project list pre-loaded by ``nav_context_middleware``."""
    return {"nav_projects": getattr(request, "nav_projects", [])}
