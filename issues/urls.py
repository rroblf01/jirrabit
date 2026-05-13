from django.urls import path

from . import views

app_name = "issues"

urlpatterns = [
    path("projects/<str:key>/new/", views.IssueCreateView.as_view(), name="create"),
    path("projects/<str:key>/list/", views.IssueListView.as_view(), name="list"),
    path("<str:key>/", views.IssueDetailView.as_view(), name="detail"),
    path("<str:key>/edit/", views.IssueUpdateView.as_view(), name="edit"),
    path("<str:key>/status/", views.ChangeStatusView.as_view(), name="change_status"),
    path("<str:key>/comment/", views.AddCommentView.as_view(), name="add_comment"),
    path("<str:key>/upload/", views.UploadAttachmentView.as_view(), name="upload"),
    path("<str:key>/watch/", views.WatchToggleView.as_view(), name="watch_toggle"),
]
