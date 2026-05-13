from django.conf import settings
from django.db import models
from django.urls import reverse


class IssueType(models.Model):
    CATEGORY = (
        ("epic", "Epic"),
        ("story", "Story"),
        ("task", "Task"),
        ("bug", "Bug"),
        ("subtask", "Subtask"),
    )
    name = models.CharField(max_length=40, unique=True)
    category = models.CharField(max_length=16, choices=CATEGORY)
    icon = models.CharField(max_length=8, default="✷")  # decorative
    color = models.CharField(max_length=20, default="#1e6fff")

    def __str__(self):
        return self.name


class Status(models.Model):
    CATEGORY = (
        ("todo", "To Do"),
        ("in_progress", "In Progress"),
        ("done", "Done"),
    )
    name = models.CharField(max_length=40, unique=True)
    category = models.CharField(max_length=16, choices=CATEGORY)
    order = models.PositiveIntegerField(default=0)
    # Allowed forward transitions. Empty = any transition allowed (open workflow).
    allowed_next = models.ManyToManyField("self", symmetrical=False, blank=True, related_name="reachable_from")

    class Meta:
        ordering = ("order", "id")
        verbose_name_plural = "statuses"

    def __str__(self):
        return self.name

    def can_transition_to(self, target: "Status") -> bool:
        if target.pk == self.pk:
            return True
        rules = list(self.allowed_next.all())
        if not rules:
            return True
        return any(r.pk == target.pk for r in rules)


class Priority(models.Model):
    name = models.CharField(max_length=20, unique=True)
    weight = models.IntegerField(default=0, help_text="Higher = more urgent")
    color = models.CharField(max_length=20, default="#1e6fff")

    class Meta:
        ordering = ("-weight",)
        verbose_name_plural = "priorities"

    def __str__(self):
        return self.name


class Label(models.Model):
    name = models.CharField(max_length=40, unique=True)
    color = models.CharField(max_length=20, default="#1e6fff")

    def __str__(self):
        return self.name


class Issue(models.Model):
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="issues")
    key = models.CharField(max_length=30, unique=True, db_index=True)
    issue_type = models.ForeignKey(IssueType, on_delete=models.PROTECT, related_name="issues")
    status = models.ForeignKey(Status, on_delete=models.PROTECT, related_name="issues")
    priority = models.ForeignKey(Priority, on_delete=models.PROTECT, related_name="issues")
    epic = models.ForeignKey(
        "projects.Epic", on_delete=models.SET_NULL, null=True, blank=True, related_name="issues"
    )
    sprint = models.ForeignKey(
        "projects.Sprint", on_delete=models.SET_NULL, null=True, blank=True, related_name="issues"
    )
    parent = models.ForeignKey(
        "self", on_delete=models.CASCADE, null=True, blank=True, related_name="subtasks"
    )
    labels = models.ManyToManyField(Label, blank=True, related_name="issues")

    summary = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reported_issues",
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_issues",
    )
    watchers = models.ManyToManyField(
        settings.AUTH_USER_MODEL, related_name="watched_issues", blank=True
    )

    story_points = models.PositiveSmallIntegerField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    # Time tracking, in minutes.
    estimate_minutes = models.PositiveIntegerField(null=True, blank=True)
    time_spent_minutes = models.PositiveIntegerField(default=0)
    time_remaining_minutes = models.PositiveIntegerField(null=True, blank=True)

    # Project-defined custom fields keyed by ``CustomFieldDef.slug``.
    custom_fields = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    rank = models.FloatField(default=0, help_text="Used for board ordering")

    class Meta:
        ordering = ("-updated_at",)
        indexes = [
            models.Index(fields=["project", "status"]),
            models.Index(fields=["assignee"]),
        ]

    def __str__(self):
        return f"{self.key} {self.summary}"

    def get_absolute_url(self):
        return reverse("issues:detail", args=[self.key])

    def save(self, *args, **kwargs):
        if not self.key:
            num = self.project.next_issue_number()
            self.key = f"{self.project.key}-{num}"
        super().save(*args, **kwargs)

    @property
    def description_html(self) -> str:
        from core.markdown import render_markdown
        return render_markdown(self.description)


class Comment(models.Model):
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    body = models.TextField()
    edited = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("created_at",)

    def __str__(self):
        return f"Comment on {self.issue.key}"

    @property
    def body_html(self) -> str:
        from core.markdown import render_markdown
        return render_markdown(self.body)


class IssueLink(models.Model):
    TYPE_CHOICES = (
        ("blocks", "bloquea"),
        ("blocked_by", "bloqueado por"),
        ("relates_to", "relacionado con"),
        ("duplicates", "duplica"),
        ("duplicated_by", "duplicado por"),
    )
    INVERSE = {
        "blocks": "blocked_by",
        "blocked_by": "blocks",
        "relates_to": "relates_to",
        "duplicates": "duplicated_by",
        "duplicated_by": "duplicates",
    }
    source = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="links_out")
    target = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="links_in")
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("source", "target", "type")
        ordering = ("type", "id")

    def __str__(self):
        return f"{self.source.key} {self.get_type_display()} {self.target.key}"


class WorkLog(models.Model):
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="worklogs")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    minutes = models.PositiveIntegerField()
    comment = models.CharField(max_length=255, blank=True)
    logged_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-logged_at",)

    def __str__(self):
        return f"{self.issue.key} +{self.minutes}min by {self.author}"


class Attachment(models.Model):
    """File attached to an issue, stored fully in the database as base64.

    No filesystem footprint: the binary content lives in ``data`` and the
    template builds a ``data:`` URL at render time for download.
    """

    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="attachments")
    filename = models.CharField(max_length=255, default="")
    content_type = models.CharField(max_length=120, default="application/octet-stream")
    size = models.PositiveIntegerField(default=0, help_text="Raw bytes before base64.")
    data = models.TextField(default="", help_text="Base64-encoded file contents.")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.filename

    @property
    def data_url(self) -> str:
        return f"data:{self.content_type};base64,{self.data}"

    @property
    def size_human(self) -> str:
        n = self.size
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"


class HistoryEntry(models.Model):
    """Audit trail of field changes on a specific issue."""

    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="history")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    field = models.CharField(max_length=40)
    old_value = models.CharField(max_length=255, blank=True)
    new_value = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name_plural = "history entries"


class AuditEntry(models.Model):
    """Project-wide activity feed. Captures any tracked event."""

    project = models.ForeignKey(
        "projects.Project", on_delete=models.CASCADE, related_name="audit"
    )
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    verb = models.CharField(max_length=40)
    target_type = models.CharField(max_length=40, help_text="Model name, e.g. issue/comment/sprint.")
    target_id = models.PositiveIntegerField(null=True, blank=True)
    target_label = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["project", "-created_at"])]
        verbose_name_plural = "audit entries"
