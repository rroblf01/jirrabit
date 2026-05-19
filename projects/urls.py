from django.urls import path

from . import views

app_name = "projects"

urlpatterns = [
    path("", views.ProjectListView.as_view(), name="list"),
    path("new/", views.ProjectCreateView.as_view(), name="create"),
    path("<str:key>/", views.ProjectDetailView.as_view(), name="detail"),
    path("<str:key>/edit/", views.ProjectUpdateView.as_view(), name="edit"),
    path("<str:key>/epics/new/", views.EpicCreateView.as_view(), name="create_epic"),
    path("<str:key>/epics/<int:pk>/", views.EpicDetailView.as_view(), name="epic_detail"),
    path("<str:key>/sprints/new/", views.SprintCreateView.as_view(), name="create_sprint"),
    path("sprints/<int:sprint_id>/start/", views.SprintStartView.as_view(), name="start_sprint"),
    path("sprints/<int:sprint_id>/close/", views.SprintCloseView.as_view(), name="close_sprint"),
    path("<str:key>/activity/", views.ProjectActivityView.as_view(), name="activity"),
    path("<str:key>/burndown/", views.ProjectBurndownView.as_view(), name="burndown"),
    path("<str:key>/reports/", views.ProjectReportsView.as_view(), name="reports"),
    path("<str:key>/planning/", views.SprintPlanningView.as_view(), name="planning"),
    path("<str:key>/dependencies/", views.ProjectDependencyGraphView.as_view(), name="dependencies"),
    path("<str:key>/roadmap/", views.ProjectRoadmapView.as_view(), name="roadmap"),
    path("<str:key>/heatmap/", views.WorkloadHeatmapView.as_view(), name="heatmap"),
    path("<str:key>/wiki/", views.ProjectWikiView.as_view(), name="wiki"),
    path("<str:key>/sla/", views.ProjectSlaView.as_view(), name="sla"),
    path("<str:key>/members/", views.ProjectMembersView.as_view(), name="members"),
    path("<str:key>/members/add/", views.ProjectMembershipAddView.as_view(), name="member_add"),
    path("<str:key>/members/<int:pk>/", views.ProjectMembershipUpdateView.as_view(), name="member_update"),
    path("<str:key>/custom-fields/", views.ProjectCustomFieldsView.as_view(), name="custom_fields"),
    path("<str:key>/custom-fields/new/", views.ProjectCustomFieldCreateView.as_view(), name="custom_field_create"),
    path("<str:key>/custom-fields/<int:pk>/delete/", views.ProjectCustomFieldDeleteView.as_view(), name="custom_field_delete"),
    path("<str:key>/webhooks/", views.ProjectWebhooksView.as_view(), name="webhooks"),
    path("<str:key>/webhooks/new/", views.ProjectWebhookCreateView.as_view(), name="webhook_create"),
    path("<str:key>/webhooks/<int:pk>/delete/", views.ProjectWebhookDeleteView.as_view(), name="webhook_delete"),
]
