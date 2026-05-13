from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone


class ProjectQuerySet(models.QuerySet):
    def filter_visible(self, user):
        if user.is_superuser:
            return self.all()
        return self.filter(models.Q(lead=user) | models.Q(members=user)).distinct()


class Project(models.Model):
    key = models.CharField(max_length=10, unique=True, help_text="Short prefix used for issue keys, e.g. WEB.")
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    lead = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="led_projects"
    )
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="projects", blank=True)
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
        await Project.objects.filter(pk=self.pk).aupdate(
            issue_counter=models.F("issue_counter") + 1
        )
        await self.arefresh_from_db(fields=["issue_counter"])
        return self.issue_counter


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
    STATUS = (("future", "Future"), ("active", "Active"), ("closed", "Closed"))
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="sprints")
    name = models.CharField(max_length=120)
    goal = models.CharField(max_length=255, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default="future")
    started_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

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

    async def aclose(self):
        self.status = "closed"
        self.closed_at = timezone.now()
        await self.asave()
