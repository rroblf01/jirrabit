from django.urls import path

from . import views

app_name = "search"

urlpatterns = [
    path("", views.SearchView.as_view(), name="search"),
    path("suggest/", views.SearchSuggestView.as_view(), name="suggest"),
    path("filters/new/", views.SavedFilterCreateView.as_view(), name="filter_create"),
    path("filters/<int:pk>/delete/", views.SavedFilterDeleteView.as_view(), name="filter_delete"),
]
