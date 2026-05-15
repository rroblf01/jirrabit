from django.urls import path

from . import workflow_views as views

app_name = "workflow"

urlpatterns = [
    path("", views.WorkflowOverviewView.as_view(), name="overview"),

    # Status
    path("statuses/", views.StatusListView.as_view(), name="status_list"),
    path("statuses/new/", views.StatusCreateView.as_view(), name="status_create"),
    path("statuses/<int:pk>/edit/", views.StatusUpdateView.as_view(), name="status_edit"),
    path("statuses/<int:pk>/delete/", views.StatusDeleteView.as_view(), name="status_delete"),
    path("statuses/<int:pk>/transitions/", views.StatusTransitionsView.as_view(), name="status_transitions"),
    path("statuses/reorder/", views.StatusReorderView.as_view(), name="status_reorder"),
    path("matrix/", views.StatusTransitionsMatrixView.as_view(), name="matrix"),

    # Priority
    path("priorities/", views.PriorityListView.as_view(), name="priority_list"),
    path("priorities/new/", views.PriorityCreateView.as_view(), name="priority_create"),
    path("priorities/<int:pk>/edit/", views.PriorityUpdateView.as_view(), name="priority_edit"),
    path("priorities/<int:pk>/delete/", views.PriorityDeleteView.as_view(), name="priority_delete"),

    # IssueType
    path("types/", views.IssueTypeListView.as_view(), name="type_list"),
    path("types/new/", views.IssueTypeCreateView.as_view(), name="type_create"),
    path("types/<int:pk>/edit/", views.IssueTypeUpdateView.as_view(), name="type_edit"),
    path("types/<int:pk>/delete/", views.IssueTypeDeleteView.as_view(), name="type_delete"),

    # Label
    path("labels/", views.LabelListView.as_view(), name="label_list"),
    path("labels/new/", views.LabelCreateView.as_view(), name="label_create"),
    path("labels/<int:pk>/edit/", views.LabelUpdateView.as_view(), name="label_edit"),
    path("labels/<int:pk>/delete/", views.LabelDeleteView.as_view(), name="label_delete"),
]
