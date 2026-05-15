from django.contrib import admin
from django.urls import include, path

from .api import api as ninja_api

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", ninja_api.urls),
    path("accounts/", include("accounts.urls", namespace="accounts")),
    path("projects/", include("projects.urls", namespace="projects")),
    path("issues/", include("issues.urls", namespace="issues")),
    path("board/", include("board.urls", namespace="board")),
    path("search/", include("search.urls", namespace="search")),
    path("workflow/", include("issues.workflow_urls", namespace="workflow")),
    path("", include("core.urls", namespace="core")),
]
