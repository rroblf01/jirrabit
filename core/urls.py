from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),
    path("help/", views.HelpView.as_view(), name="help"),
    path("md/preview/", views.MarkdownPreviewView.as_view(), name="md_preview"),
    path("dashboard/config/", views.DashboardConfigView.as_view(), name="dashboard_config"),
]
