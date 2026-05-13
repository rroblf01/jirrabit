from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls", namespace="accounts")),
    path("projects/", include("projects.urls", namespace="projects")),
    path("issues/", include("issues.urls", namespace="issues")),
    path("board/", include("board.urls", namespace="board")),
    path("search/", include("search.urls", namespace="search")),
    path("", include("core.urls", namespace="core")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
