from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class ProjectQuerySet(models.QuerySet):
    def filter_visible(self, user):
        if user.is_superuser:
            return self.all()
        return self.filter(models.Q(lead=user) | models.Q(memberships__user=user)).distinct()


class Project(models.Model):
    key = models.CharField(
        max_length=10, unique=True, help_text="Short prefix used for issue keys, e.g. WEB."
    )
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    lead = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="led_projects")
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="projects",
        through="ProjectMembership",
        blank=True,
    )
    archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    issue_counter = models.PositiveIntegerField(default=0)

    objects = ProjectQuerySet.as_manager()

    class Meta:
        ordering = ("key",)

    def __str__(self):
        return f"{self.key} — {self.name}"

    def get_absolute_url(self):
        return reverse("projects:detail", args=[self.key])

    def next_issue_number(self):
        self.issue_counter = models.F("issue_counter") + 1
        self.save(update_fields=["issue_counter"])
        self.refresh_from_db(fields=["issue_counter"])
        return self.issue_counter

    async def anext_issue_number(self):
        # One of two deliberate aupdate() calls in the project. This one must
        # stay: it is a counter bump on the hot path of every Issue.save(), and
        # going through asave() would fire the Project post_save receivers —
        # an audit row and a "project updated" notification every single time
        # anybody created an issue. The counter is an implementation detail, not
        # a user-visible edit, so being signal-free is the correct behaviour.
        # See board/views.py::_asave_each for the opposite case.
        await Project.objects.filter(pk=self.pk).aupdate(issue_counter=models.F("issue_counter") + 1)
        await self.arefresh_from_db(fields=["issue_counter"])
        return self.issue_counter


class ProjectMembership(models.Model):
    ROLE_CHOICES = (
        ("admin", _("Admin")),
        ("member", _("Member")),
        ("viewer", _("Viewer")),
    )
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default="member")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("project", "user")
        ordering = ("user__username",)

    def __str__(self):
        return f"{self.user} @ {self.project.key} ({self.role})"


class ProjectWiki(models.Model):
    """Markdown wiki page attached to a project. One row per project."""

    project = models.OneToOneField(
        Project,
        on_delete=models.CASCADE,
        related_name="wiki",
    )
    body = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    def __str__(self):
        return f"Wiki: {self.project.key}"


class SavedFilter(models.Model):
    SCOPE_CHOICES = (("private", _("Privado")), ("shared", _("Compartido")))
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_filters"
    )
    name = models.CharField(max_length=120)
    query = models.TextField(help_text="JQL-lite query expression.")
    scope = models.CharField(max_length=10, choices=SCOPE_CHOICES, default="private")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return f"{self.name} ({self.owner})"


class Webhook(models.Model):
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="webhooks", null=True, blank=True
    )
    name = models.CharField(max_length=80)
    action = models.CharField(
        max_length=120,
        default="",
        help_text="Action code from the in-process webhook-action registry.",
    )
    entity = models.CharField(
        max_length=32,
        default="issue",
        help_text="What kind of object triggers this hook (issue/epic/comment).",
    )
    event = models.CharField(
        max_length=64,
        default="issue.updated",
        help_text="Event code from the webhook registry.",
    )
    state_filter = models.CharField(
        max_length=255,
        blank=True,
        help_text="Comma-separated state names. Empty = fire on any state.",
    )
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_status = models.IntegerField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    last_delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("project", "name")

    def __str__(self):
        scope = self.project.key if self.project_id else "global"
        return f"{scope}:{self.name}"

    def listens_to(self, event: str, current_state: str | None = None) -> bool:
        if not self.active:
            return False
        if self.event != event and self.event != "*":
            return False
        if not self.state_filter:
            return True
        if current_state is None:
            return True
        wanted = {s.strip() for s in self.state_filter.split(",") if s.strip()}
        return current_state in wanted


class CustomFieldDef(models.Model):
    TYPE_CHOICES = (
        ("text", _("Texto corto")),
        ("textarea", _("Texto largo")),
        ("number", _("Número")),
        ("select", _("Lista")),
        ("date", _("Fecha")),
        ("user", _("Usuario")),
        ("checkbox", _("Checkbox")),
    )
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="custom_fields")
    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=80)
    type = models.CharField(max_length=16, choices=TYPE_CHOICES, default="text")
    required = models.BooleanField(default=False)
    options = models.TextField(
        blank=True,
        help_text="For 'select' type: comma-separated options.",
    )
    order = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("project", "slug")
        ordering = ("order", "id")

    def __str__(self):
        return f"{self.project.key}::{self.name}"

    @property
    def option_list(self) -> list[str]:
        return [o.strip() for o in self.options.split(",") if o.strip()]


class Epic(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="epics")
    name = models.CharField(max_length=200)
    summary = models.TextField(blank=True)
    color = models.CharField(max_length=20, default="#1e6fff")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="created_epics"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    done = models.BooleanField(default=False)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return self.name


class Sprint(models.Model):
    STATUS = (("future", _("Future")), ("active", _("Active")), ("closed", _("Closed")))
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="sprints")
    name = models.CharField(max_length=120)
    goal = models.CharField(max_length=255, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default="future")
    started_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    retro_notes = models.TextField(
        blank=True,
        default="",
        help_text=_("Notas de retrospectiva: qué fue bien, qué mejorar."),
    )

    class Meta:
        ordering = ("-start_date", "-id")

    def __str__(self):
        return f"{self.project.key} · {self.name}"

    def start(self):
        self.status = "active"
        self.started_at = timezone.now()
        self.save()

    async def astart(self):
        self.status = "active"
        self.started_at = timezone.now()
        await self.asave()

    def close(self):
        self.status = "closed"
        self.closed_at = timezone.now()
        self.save()

    async def aclose(self, carry_to=None):
        """Close the sprint. If ``carry_to`` is given, move incomplete (non-done)
        issues to that sprint; otherwise send them back to backlog."""
        from issues.models import Issue

        self.status = "closed"
        self.closed_at = timezone.now()
        await self.asave()
        incomplete = (
            Issue.objects.filter(sprint=self)
            .exclude(status__category="done")
            .select_related("project", "status")
        )
        new_sprint_id = carry_to.pk if carry_to is not None else None
        # One asave per row rather than aupdate(sprint_id=...). QuerySet.aupdate
        # is silent about post_save, and four receivers hang off it —
        # notifications, audit, webhooks and the realtime board broadcast. A bulk
        # aupdate would move every issue off the sprint while leaving watchers
        # un-notified, the audit log empty and the board stale until a manual
        # refresh. select_related keeps the receivers from issuing a query per
        # row for instance.project.
        async for issue in incomplete:
            issue.sprint_id = new_sprint_id
            await issue.asave(update_fields=["sprint_id", "updated_at"])


def aclose_sprint_atomic(sprint_pk, carry_to_pk):
    """Close a sprint and carry its unfinished issues, in one transaction.

    The async :meth:`Sprint.aclose` cannot be this: it is ``async def``, and
    entering ``transaction.atomic()`` from a coroutine raises
    ``SynchronousOnlyOperation``, so it saves the sprint and then walks the
    issues with an ``await`` between each. A failure partway leaves a closed
    sprint with only *some* of its issues carried over and no record of which —
    an inconsistent state that no caller can tell apart from success.

    Doing it synchronously puts the whole unit in one unbroken block, which is
    also the only way the row lock is worth taking. It reaches the loop through
    ``sync_to_async(..., thread_sensitive=True)``, the same chokepoint
    ``issues.views._change_status_atomic`` uses.

    ``carry_to_pk`` is validated here rather than by the caller, which is the
    point: the web view checks it and the model did not, so calling ``aclose``
    directly could move a sprint's issues into another project's sprint or into
    an already-closed one. One place decides, and it is the place every caller
    goes through.

    Returns ``(sprint, moved_count)``.
    """
    from django.db import transaction

    from issues.models import Issue

    with transaction.atomic():
        sprint = Sprint.objects.select_for_update().get(pk=sprint_pk)
        carry_to = None
        if carry_to_pk is not None:
            # Scoped to the same project on purpose: the destination is a place
            # to put this sprint's unfinished work, and a sprint in another
            # project is not one. The web view checked this and the model did
            # not, so calling aclose directly could cross projects.
            carry_to = Sprint.objects.filter(pk=carry_to_pk, project_id=sprint.project_id).first()
            if carry_to is None:
                raise ValueError("el sprint destino no existe o es de otro proyecto")
            if carry_to.pk == sprint.pk:
                raise ValueError("un sprint no puede trasladarse a sí mismo")
            if carry_to.status == "closed":
                raise ValueError("el sprint destino está cerrado")

        sprint.status = "closed"
        sprint.closed_at = timezone.now()
        sprint.save()

        incomplete = list(
            Issue.objects.filter(sprint_id=sprint.pk)
            .exclude(status__category="done")
            .select_related("project", "status")
        )
        new_sprint_id = carry_to.pk if carry_to is not None else None
        # One save per row rather than update(sprint_id=...): the four
        # post_save receivers — notifications, audit, webhooks and the realtime
        # broadcast — hang off the row save and are silent for a queryset
        # update, so a bulk write would move every issue while leaving watchers
        # un-notified and the audit log empty. select_related keeps the
        # receivers from firing a query per row for instance.project.
        for issue in incomplete:
            issue.sprint_id = new_sprint_id
            issue.save(update_fields=["sprint_id", "updated_at"])
    return sprint, len(incomplete)
