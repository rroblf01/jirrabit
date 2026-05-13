from asgiref.sync import iscoroutinefunction
from django.utils.decorators import sync_and_async_middleware


@sync_and_async_middleware
def nav_context_middleware(get_response):
    """Pre-load nav projects onto ``request`` so the context processor
    stays DB-free and works in async views without sync iteration."""

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
