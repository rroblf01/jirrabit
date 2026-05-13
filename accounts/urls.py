from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.JirrabitLoginView.as_view(), name="login"),
    path("logout/", views.JirrabitLogoutView.as_view(), name="logout"),
    path("register/", views.RegisterView.as_view(), name="register"),
    path("profile/", views.ProfileView.as_view(), name="profile"),
    path("users/", views.UserListView.as_view(), name="user_list"),
]
