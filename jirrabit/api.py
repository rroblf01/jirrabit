"""Public REST API powered by django-ninja.

Mounted at ``/api/v1/`` from :mod:`jirrabit.urls`. The ``/v1/`` prefix
gives us room to break schemas in a future ``v2`` without breaking
existing clients. Two auth methods are accepted on every endpoint:

- session cookie (used by the web UI through ``django_auth``).
- ``Authorization: Bearer <token>`` for headless / scripted access; the
  token is matched against ``accounts.APIKey.token_hash``.

List endpoints accept ``page`` (1-based) and ``size`` (default 50, max
200) query params and wrap items in a ``Page`` envelope.
"""
from datetime import date as _date
from typing import Generic, List, Optional, TypeVar

from django.db import models
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import ModelSchema, NinjaAPI, Schema
from ninja.security import HttpBearer, django_auth

from accounts.models import APIKey, User
from issues.models import Comment, Issue, IssueType, Priority, Status, WorkLog
from projects.models import Project, Sprint


def _visible_project(request, key: str) -> Project:
    """Return ``Project`` by key only if the requesting user can see it.

    Wraps ``filter_visible`` so non-members get 404 (rather than 403,
    avoiding the leak of which project keys exist).
    """
    qs = Project.objects.filter_visible(request.user)
    try:
        return qs.get(key=key)
    except Project.DoesNotExist as exc:
        raise Http404 from exc


def _visible_issue(request, key: str) -> Issue:
    """Same as ``_visible_project`` at the issue level."""
    visible = Project.objects.filter_visible(request.user)
    qs = Issue.objects.filter(project__in=visible).select_related(
        "project", "status", "priority", "issue_type", "assignee", "reporter"
    )
    try:
        return qs.get(key=key)
    except Issue.DoesNotExist as exc:
        raise Http404 from exc


class APIKeyAuth(HttpBearer):
    """Bearer token auth backed by ``accounts.APIKey``."""

    def authenticate(self, request, token: str):
        if not token:
            return None
        try:
            key = APIKey.objects.select_related("owner").get(
                token_hash=APIKey.hash_token(token),
                revoked_at__isnull=True,
            )
        except APIKey.DoesNotExist:
            return None
        APIKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
        request.user = key.owner
        return key.owner


api = NinjaAPI(
    title="Jirrabit API",
    version="1.0",
    urls_namespace="api-v1",
    auth=[django_auth, APIKeyAuth()],
)


# --- pagination helpers --------------------------------------------------

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

T = TypeVar("T")


class Page(Schema, Generic[T]):
    """Generic list-envelope returned by list endpoints."""

    count: int
    page: int
    size: int
    pages: int
    next: Optional[int] = None
    previous: Optional[int] = None
    items: List[T]


def paginate(queryset, builder, page: int, size: int) -> dict:
    """Slice ``queryset`` and wrap into a ``Page`` payload.

    ``builder(item)`` converts each row to the right schema.
    """
    page = max(int(page or 1), 1)
    size = max(1, min(int(size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    total = queryset.count()
    offset = (page - 1) * size
    items = [builder(o) for o in queryset[offset : offset + size]]
    pages = max(1, (total + size - 1) // size)
    return {
        "count": total,
        "page": page,
        "size": size,
        "pages": pages,
        "next": page + 1 if page < pages else None,
        "previous": page - 1 if page > 1 else None,
        "items": items,
    }


# --- schemas -------------------------------------------------------------

class UserOut(ModelSchema):
    class Meta:
        model = User
        fields = ["id", "username", "display_name", "email"]


class ProjectOut(ModelSchema):
    class Meta:
        model = Project
        fields = ["id", "key", "name", "description", "archived"]


class SprintOut(ModelSchema):
    class Meta:
        model = Sprint
        fields = ["id", "name", "goal", "status", "start_date", "end_date", "retro_notes"]


class SprintIn(Schema):
    name: Optional[str] = None
    goal: Optional[str] = None
    start_date: Optional[_date] = None
    end_date: Optional[_date] = None
    retro_notes: Optional[str] = None


class ProjectIn(Schema):
    name: Optional[str] = None
    description: Optional[str] = None
    archived: Optional[bool] = None


class WorkLogOut(Schema):
    id: int
    issue: str
    author: str
    minutes: int
    comment: str
    logged_at: str

    @staticmethod
    def from_log(w: WorkLog) -> "WorkLogOut":
        return WorkLogOut(
            id=w.pk, issue=w.issue.key, author=str(w.author),
            minutes=w.minutes, comment=w.comment, logged_at=w.logged_at.isoformat(),
        )


class WorkLogIn(Schema):
    minutes: int
    comment: str = ""


class IssueOut(Schema):
    id: int
    key: str
    summary: str
    description: str
    status: str
    priority: str
    type: str
    project: str
    assignee: Optional[str] = None
    reporter: Optional[str] = None
    story_points: Optional[int] = None
    due_date: Optional[_date] = None
    estimate_minutes: Optional[int] = None
    time_spent_minutes: int = 0

    @staticmethod
    def from_issue(i: Issue) -> "IssueOut":
        return IssueOut(
            id=i.pk, key=i.key, summary=i.summary, description=i.description,
            status=str(i.status), priority=str(i.priority), type=str(i.issue_type),
            project=i.project.key,
            assignee=getattr(i.assignee, "username", None),
            reporter=getattr(i.reporter, "username", None),
            story_points=i.story_points, due_date=i.due_date,
            estimate_minutes=i.estimate_minutes, time_spent_minutes=i.time_spent_minutes,
        )


class IssueIn(Schema):
    summary: str
    description: str = ""
    issue_type_id: Optional[int] = None
    status_id: Optional[int] = None
    priority_id: Optional[int] = None
    assignee_id: Optional[int] = None
    sprint_id: Optional[int] = None
    story_points: Optional[int] = None
    due_date: Optional[_date] = None


class IssuePatch(Schema):
    summary: Optional[str] = None
    description: Optional[str] = None
    status_id: Optional[int] = None
    priority_id: Optional[int] = None
    assignee_id: Optional[int] = None
    sprint_id: Optional[int] = None
    story_points: Optional[int] = None
    due_date: Optional[_date] = None


class CommentOut(Schema):
    id: int
    issue: str
    author: str
    body: str
    created_at: str
    edited: bool

    @staticmethod
    def from_comment(c: Comment) -> "CommentOut":
        return CommentOut(
            id=c.pk, issue=c.issue.key, author=str(c.author),
            body=c.body, created_at=c.created_at.isoformat(), edited=c.edited,
        )


class CommentIn(Schema):
    body: str


# --- endpoints -----------------------------------------------------------

@api.get("/projects/", response=Page[ProjectOut])
def list_projects(request, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    qs = Project.objects.filter_visible(request.user).order_by("key")
    return paginate(qs, lambda p: ProjectOut.from_orm(p), page, size)


@api.get("/projects/{key}/", response=ProjectOut)
def get_project(request, key: str):
    return _visible_project(request, key)


@api.get("/projects/{key}/sprints/", response=Page[SprintOut])
def list_sprints(request, key: str, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    project = _visible_project(request, key)
    return paginate(project.sprints.all(), lambda s: SprintOut.from_orm(s), page, size)


@api.get("/projects/{key}/issues/", response=Page[IssueOut])
def list_issues(
    request,
    key: str,
    page: int = 1,
    size: int = DEFAULT_PAGE_SIZE,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
):
    project = _visible_project(request, key)
    qs = project.issues.select_related(
        "status", "priority", "issue_type", "assignee", "reporter", "project"
    ).order_by("-updated_at")
    if status:
        qs = qs.filter(status__name__iexact=status)
    if assignee:
        qs = qs.filter(assignee__username=assignee)
    return paginate(qs, IssueOut.from_issue, page, size)


def _validate_assignee(project, user_id):
    if user_id is None:
        return None
    in_project = (
        User.objects.filter(pk=user_id, is_active=True).filter(
            models.Q(memberships__project=project) | models.Q(led_projects=project)
        ).exists()
    )
    if not in_project:
        from ninja.errors import HttpError
        raise HttpError(400, "assignee no pertenece al proyecto")
    return user_id


def _validate_sprint(project, sprint_id):
    if sprint_id is None:
        return None
    if not Sprint.objects.filter(pk=sprint_id, project=project).exists():
        from ninja.errors import HttpError
        raise HttpError(400, "sprint no pertenece al proyecto")
    return sprint_id


@api.post("/projects/{key}/issues/", response=IssueOut)
def create_issue(request, key: str, payload: IssueIn):
    from ninja.errors import HttpError
    project = _visible_project(request, key)
    try:
        status = (
            Status.objects.get(pk=payload.status_id) if payload.status_id
            else Status.objects.order_by("order").first()
        )
        priority = (
            Priority.objects.get(pk=payload.priority_id) if payload.priority_id
            else Priority.objects.first()
        )
        itype = (
            IssueType.objects.get(pk=payload.issue_type_id) if payload.issue_type_id
            else IssueType.objects.first()
        )
    except (Status.DoesNotExist, Priority.DoesNotExist, IssueType.DoesNotExist) as exc:
        raise HttpError(400, "status/priority/type inválido") from exc
    assignee_id = _validate_assignee(project, payload.assignee_id)
    sprint_id = _validate_sprint(project, payload.sprint_id)
    issue = Issue.objects.create(
        project=project, reporter=request.user, summary=payload.summary,
        description=payload.description, status=status, priority=priority, issue_type=itype,
        assignee_id=assignee_id, sprint_id=sprint_id,
        story_points=payload.story_points, due_date=payload.due_date,
    )
    return IssueOut.from_issue(issue)


@api.get("/issues/{key}/", response=IssueOut)
def get_issue(request, key: str):
    return IssueOut.from_issue(_visible_issue(request, key))


_PATCHABLE_FIELDS = {
    "summary", "description", "status_id", "priority_id",
    "assignee_id", "sprint_id", "story_points", "due_date",
}


@api.patch("/issues/{key}/", response=IssueOut)
def patch_issue(request, key: str, payload: IssuePatch):
    from ninja.errors import HttpError
    issue = _visible_issue(request, key)
    data = payload.dict(exclude_unset=True)
    if "status_id" in data:
        if not Status.objects.filter(pk=data["status_id"]).exists():
            raise HttpError(400, "status inválido")
    if "priority_id" in data:
        if not Priority.objects.filter(pk=data["priority_id"]).exists():
            raise HttpError(400, "priority inválido")
    if "assignee_id" in data:
        _validate_assignee(issue.project, data["assignee_id"])
    if "sprint_id" in data:
        _validate_sprint(issue.project, data["sprint_id"])
    for field, value in data.items():
        if field not in _PATCHABLE_FIELDS:
            continue
        setattr(issue, field, value)
    issue.save()
    issue.refresh_from_db()
    return IssueOut.from_issue(issue)


@api.delete("/issues/{key}/")
def delete_issue(request, key: str):
    issue = _visible_issue(request, key)
    issue.delete()
    return {"deleted": key}


@api.get("/issues/{key}/comments/", response=Page[CommentOut])
def list_comments(request, key: str, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    issue = _visible_issue(request, key)
    qs = issue.comments.select_related("author").order_by("created_at")
    return paginate(qs, CommentOut.from_comment, page, size)


@api.post("/issues/{key}/comments/", response=CommentOut)
def add_comment(request, key: str, payload: CommentIn):
    issue = _visible_issue(request, key)
    c = Comment.objects.create(issue=issue, author=request.user, body=payload.body)
    return CommentOut.from_comment(c)


@api.get("/me/", response=UserOut)
def me(request):
    return request.user


# --- project mgmt ---

_PROJECT_PATCHABLE = {"name", "description", "archived"}


def _assert_project_admin(request, project: Project) -> None:
    """Raise ninja HttpError(403) if the user isn't admin/lead on the project."""
    from ninja.errors import HttpError
    if request.user.is_superuser or project.lead_id == request.user.pk:
        return
    from projects.models import ProjectMembership
    is_admin = ProjectMembership.objects.filter(
        project=project, user=request.user, role="admin",
    ).exists()
    if not is_admin:
        raise HttpError(403, "Requiere rol admin en el proyecto")


@api.patch("/projects/{key}/", response=ProjectOut)
def patch_project(request, key: str, payload: ProjectIn):
    project = _visible_project(request, key)
    _assert_project_admin(request, project)
    data = payload.dict(exclude_unset=True)
    for field, value in data.items():
        if field in _PROJECT_PATCHABLE:
            setattr(project, field, value)
    project.save()
    return project


@api.delete("/projects/{key}/")
def delete_project(request, key: str):
    project = _visible_project(request, key)
    _assert_project_admin(request, project)
    project.delete()
    return {"deleted": key}


# --- sprint mgmt ---

@api.get("/sprints/{sprint_id}/", response=SprintOut)
def get_sprint(request, sprint_id: int):
    from django.http import Http404
    visible = Project.objects.filter_visible(request.user)
    try:
        return Sprint.objects.select_related("project").get(
            pk=sprint_id, project__in=visible,
        )
    except Sprint.DoesNotExist as exc:
        raise Http404 from exc


@api.patch("/sprints/{sprint_id}/", response=SprintOut)
def patch_sprint(request, sprint_id: int, payload: SprintIn):
    from django.http import Http404
    try:
        sprint = Sprint.objects.select_related("project").get(pk=sprint_id)
    except Sprint.DoesNotExist as exc:
        raise Http404 from exc
    if not Project.objects.filter_visible(request.user).filter(pk=sprint.project_id).exists():
        raise Http404
    _assert_project_admin(request, sprint.project)
    for field, value in payload.dict(exclude_unset=True).items():
        setattr(sprint, field, value)
    sprint.save()
    return sprint


@api.delete("/sprints/{sprint_id}/")
def delete_sprint(request, sprint_id: int):
    from django.http import Http404
    try:
        sprint = Sprint.objects.select_related("project").get(pk=sprint_id)
    except Sprint.DoesNotExist as exc:
        raise Http404 from exc
    _assert_project_admin(request, sprint.project)
    sprint.delete()
    return {"deleted": sprint_id}


# --- worklogs ---

@api.get("/issues/{key}/worklogs/", response=Page[WorkLogOut])
def list_worklogs(request, key: str, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    issue = _visible_issue(request, key)
    qs = issue.worklogs.select_related("author").order_by("-logged_at")
    return paginate(qs, WorkLogOut.from_log, page, size)


@api.post("/issues/{key}/worklogs/", response=WorkLogOut)
def add_worklog(request, key: str, payload: WorkLogIn):
    from ninja.errors import HttpError
    issue = _visible_issue(request, key)
    if payload.minutes <= 0:
        raise HttpError(400, "minutes debe ser > 0")
    from django.db import transaction
    with transaction.atomic():
        locked = Issue.objects.select_for_update().get(pk=issue.pk)
        log = WorkLog.objects.create(
            issue=locked, author=request.user,
            minutes=payload.minutes, comment=payload.comment[:255],
        )
        locked.time_spent_minutes = (locked.time_spent_minutes or 0) + payload.minutes
        if locked.time_remaining_minutes:
            locked.time_remaining_minutes = max(
                0, locked.time_remaining_minutes - payload.minutes,
            )
        locked.save(update_fields=[
            "time_spent_minutes", "time_remaining_minutes", "updated_at",
        ])
    return WorkLogOut.from_log(log)
