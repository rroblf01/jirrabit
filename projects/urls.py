from django.urls import path

from . import views

app_name = "projects"

urlpatterns = [
    path("", views.ProjectListView.as_view(), name="list"),
    path("new/", views.ProjectCreateView.as_view(), name="create"),
    path("<str:key>/", views.ProjectDetailView.as_view(), name="detail"),
    path("<str:key>/edit/", views.ProjectUpdateView.as_view(), name="edit"),
    path("<str:key>/epics/new/", views.EpicCreateView.as_view(), name="create_epic"),
    path("<str:key>/sprints/new/", views.SprintCreateView.as_view(), name="create_sprint"),
    path("sprints/<int:sprint_id>/start/", views.SprintStartView.as_view(), name="start_sprint"),
    path("sprints/<int:sprint_id>/close/", views.SprintCloseView.as_view(), name="close_sprint"),
]
