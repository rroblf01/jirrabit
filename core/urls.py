from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),
    path("hooks/", views.HooksAdminView.as_view(), name="hooks"),
]
