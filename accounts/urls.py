from django.urls import path

from . import admin_views, views

app_name = "accounts"

urlpatterns = [
    path("login/", views.JirrabitLoginView.as_view(), name="login"),
    path("logout/", views.JirrabitLogoutView.as_view(), name="logout"),
    path("register/", views.RegisterView.as_view(), name="register"),
    path("profile/", views.ProfileView.as_view(), name="profile"),
    path("users/", views.UserListView.as_view(), name="user_list"),
    path("admin/users/", admin_views.AdminUserListView.as_view(), name="admin_user_list"),
    path("admin/users/new/", admin_views.AdminUserCreateView.as_view(), name="admin_user_create"),
    path("admin/users/<int:pk>/edit/", admin_views.AdminUserEditView.as_view(), name="admin_user_edit"),
    path("admin/users/<int:pk>/toggle/", admin_views.AdminUserToggleActiveView.as_view(), name="admin_user_toggle"),
    path("notifications/", views.NotificationInboxView.as_view(), name="notifications"),
    path("notifications/mark-read/", views.NotificationMarkReadView.as_view(), name="notifications_mark_read"),
    path("mentions/search/", views.UserMentionSearchView.as_view(), name="mention_search"),
]
