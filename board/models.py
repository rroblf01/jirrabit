"""Board-app-local models.

The board itself is a virtual view over Issues + Projects, but per-user
preferences (saved filter combinations) live here.
"""
from django.conf import settings
from django.db import models


class SavedBoardView(models.Model):
    """A named filter combination for the board.

    The ``filters`` field stores a JSON dict matching the board GET params
    (``assignee``, ``type``, ``priority``, ``epic``, ``sprint``, ``stale``,
    ``due``, ``text``). Re-applying the view renders the URL with those
    params so existing filter logic works unchanged.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="board_views",
    )
    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="board_views",
    )
    name = models.CharField(max_length=80)
    filters = models.JSONField(default=dict, blank=True)
    is_default = models.BooleanField(default=False,
        help_text="Auto-applied when the user opens this project's board.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("name",)
        unique_together = ("user", "project", "name")
        indexes = [models.Index(fields=["user", "project"])]

    def __str__(self):
        return f"{self.user.username}/{self.project.key}:{self.name}"
