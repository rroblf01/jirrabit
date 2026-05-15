from django.urls import path

from . import views

app_name = "board"

urlpatterns = [
    path("<str:key>/", views.BoardView.as_view(), name="board"),
    path("<str:key>/bulk/", views.BulkUpdateView.as_view(), name="bulk_update"),
    path("<str:key>/backlog/", views.BacklogView.as_view(), name="backlog"),
    path("<str:key>/column-create/", views.BoardColumnQuickCreateView.as_view(),
         name="column_create"),
    path("<str:key>/views/", views.BoardViewListView.as_view(), name="view_list"),
    path("<str:key>/views/save/", views.BoardViewSaveView.as_view(), name="view_save"),
    path("<str:key>/views/<int:pk>/delete/", views.BoardViewDeleteView.as_view(),
         name="view_delete"),
    path("card/<str:key>/move/", views.MoveCardView.as_view(), name="move_card"),
    path("card/<str:key>/sprint/", views.BacklogMoveView.as_view(), name="move_sprint"),
]
