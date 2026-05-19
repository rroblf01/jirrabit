from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from . import admin_views, views

app_name = "accounts"

urlpatterns = [
    path("login/", views.JirrabitLoginView.as_view(), name="login"),
    path("logout/", views.JirrabitLogoutView.as_view(), name="logout"),
    path("logout-all/", views.LogoutAllDevicesView.as_view(), name="logout_all"),
    path("register/", views.RegisterView.as_view(), name="register"),
    path(
        "password-reset/",
        auth_views.PasswordResetView.as_view(
            template_name="accounts/password_reset.html",
            email_template_name="accounts/password_reset_email.txt",
            subject_template_name="accounts/password_reset_subject.txt",
            success_url=reverse_lazy("accounts:password_reset_done"),
        ),
        name="password_reset",
    ),
    path(
        "password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="accounts/password_reset_done.html",
        ),
        name="password_reset_done",
    ),
    path(
        "password-reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="accounts/password_reset_confirm.html",
            success_url=reverse_lazy("accounts:password_reset_complete"),
        ),
        name="password_reset_confirm",
    ),
    path(
        "password-reset/complete/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="accounts/password_reset_complete.html",
        ),
        name="password_reset_complete",
    ),
    path("profile/", views.ProfileView.as_view(), name="profile"),
    path("users/", views.UserListView.as_view(), name="user_list"),
    path("users/<str:username>/card/", views.UserCardView.as_view(), name="user_card"),
    path("admin/users/", admin_views.AdminUserListView.as_view(), name="admin_user_list"),
    path("admin/users/new/", admin_views.AdminUserCreateView.as_view(), name="admin_user_create"),
    path("admin/users/<int:pk>/edit/", admin_views.AdminUserEditView.as_view(), name="admin_user_edit"),
    path("admin/users/<int:pk>/toggle/", admin_views.AdminUserToggleActiveView.as_view(), name="admin_user_toggle"),
    path("admin/invites/", admin_views.AdminInviteListView.as_view(), name="admin_invite_list"),
    path("admin/invites/new/", admin_views.AdminInviteCreateView.as_view(), name="admin_invite_create"),
    path("admin/invites/<int:pk>/revoke/", admin_views.AdminInviteRevokeView.as_view(), name="admin_invite_revoke"),
    path("admin/teams/", admin_views.AdminTeamListView.as_view(), name="admin_team_list"),
    path("admin/teams/new/", admin_views.AdminTeamCreateView.as_view(), name="admin_team_create"),
    path("admin/teams/<int:pk>/edit/", admin_views.AdminTeamEditView.as_view(), name="admin_team_edit"),
    path("admin/teams/<int:pk>/delete/", admin_views.AdminTeamDeleteView.as_view(), name="admin_team_delete"),
    path("teams/<slug:slug>/", admin_views.TeamDetailView.as_view(), name="team_detail"),
    path("api-keys/", views.APIKeyListView.as_view(), name="api_keys"),
    path("api-keys/new/", views.APIKeyCreateView.as_view(), name="api_key_create"),
    path("api-keys/<int:pk>/revoke/", views.APIKeyRevokeView.as_view(), name="api_key_revoke"),
    path("notifications/", views.NotificationInboxView.as_view(), name="notifications"),
    path("notifications/count/", views.NotificationCountView.as_view(), name="notifications_count"),
    path("notifications/mark-read/", views.NotificationMarkReadView.as_view(), name="notifications_mark_read"),
    path("mentions/search/", views.UserMentionSearchView.as_view(), name="mention_search"),
    path("palette-preview/", views.PalettePreviewView.as_view(), name="palette_preview"),
]
