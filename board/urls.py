from django.urls import path

from . import views

app_name = "board"

urlpatterns = [
    path("<str:key>/", views.BoardView.as_view(), name="board"),
    path("card/<str:key>/move/", views.MoveCardView.as_view(), name="move_card"),
]
