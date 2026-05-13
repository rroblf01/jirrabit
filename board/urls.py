from django.urls import path

from . import views

app_name = "board"

urlpatterns = [
    path("<str:key>/", views.BoardView.as_view(), name="board"),
    path("<str:key>/bulk/", views.BulkUpdateView.as_view(), name="bulk_update"),
    path("<str:key>/backlog/", views.BacklogView.as_view(), name="backlog"),
    path("card/<str:key>/move/", views.MoveCardView.as_view(), name="move_card"),
    path("card/<str:key>/sprint/", views.BacklogMoveView.as_view(), name="move_sprint"),
]
