from django.urls import path

from . import views

app_name = "issues"

urlpatterns = [
    path("projects/<str:key>/new/", views.IssueCreateView.as_view(), name="create"),
    path("projects/<str:key>/list/", views.IssueListView.as_view(), name="list"),
    path("projects/<str:key>/export.csv", views.IssueCsvExportView.as_view(), name="export_csv"),
    path("projects/<str:key>/import-csv/", views.IssueCsvImportView.as_view(), name="import_csv"),
    path("<str:key>/", views.IssueDetailView.as_view(), name="detail"),
    path("<str:key>/edit/", views.IssueUpdateView.as_view(), name="edit"),
    path("<str:key>/status/", views.ChangeStatusView.as_view(), name="change_status"),
    path("<str:key>/advance/", views.AdvanceStatusView.as_view(), name="advance_status"),
    path("<str:key>/clone/", views.IssueCloneView.as_view(), name="clone"),
    path("<str:key>/snooze/", views.SnoozeView.as_view(), name="snooze"),
    path("<str:key>/unsnooze/", views.UnsnoozeView.as_view(), name="unsnooze"),
    path("<str:key>/timer/start/", views.TimerStartView.as_view(), name="timer_start"),
    path("<str:key>/timer/stop/", views.TimerStopView.as_view(), name="timer_stop"),
    path("pin/<str:kind>/<int:pk>/", views.PinToggleView.as_view(), name="pin_toggle"),
    path("<str:key>/comment/", views.AddCommentView.as_view(), name="add_comment"),
    path("<str:key>/upload/", views.UploadAttachmentView.as_view(), name="upload"),
    path("<str:key>/watch/", views.WatchToggleView.as_view(), name="watch_toggle"),
    path("<str:key>/inline/<str:field>/edit/", views.InlineEditFormView.as_view(), name="inline_edit_form"),
    path("<str:key>/inline/<str:field>/", views.InlineEditApplyView.as_view(), name="inline_edit"),
    path("<str:key>/log-work/", views.LogWorkView.as_view(), name="log_work"),
    path("<str:key>/link/", views.IssueLinkCreateView.as_view(), name="link_create"),
    path("<str:key>/link/<int:link_id>/delete/", views.IssueLinkDeleteView.as_view(), name="link_delete"),
    path("comment/<int:pk>/edit/", views.CommentEditView.as_view(), name="comment_edit"),
    path("comment/<int:pk>/delete/", views.CommentDeleteView.as_view(), name="comment_delete"),
]
