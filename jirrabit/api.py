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

from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import ModelSchema, NinjaAPI, Schema
from ninja.security import HttpBearer, django_auth

from accounts.models import APIKey, User
from issues.models import Comment, Issue, IssueType, Priority, Status
from projects.models import Project, Sprint


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
        fields = ["id", "name", "goal", "status", "start_date", "end_date"]


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
    return get_object_or_404(Project, key=key)


@api.get("/projects/{key}/sprints/", response=Page[SprintOut])
def list_sprints(request, key: str, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    project = get_object_or_404(Project, key=key)
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
    project = get_object_or_404(Project, key=key)
    qs = project.issues.select_related(
        "status", "priority", "issue_type", "assignee", "reporter", "project"
    ).order_by("-updated_at")
    if status:
        qs = qs.filter(status__name__iexact=status)
    if assignee:
        qs = qs.filter(assignee__username=assignee)
    return paginate(qs, IssueOut.from_issue, page, size)


@api.post("/projects/{key}/issues/", response=IssueOut)
def create_issue(request, key: str, payload: IssueIn):
    project = get_object_or_404(Project, key=key)
    status = Status.objects.get(pk=payload.status_id) if payload.status_id else Status.objects.order_by("order").first()
    priority = Priority.objects.get(pk=payload.priority_id) if payload.priority_id else Priority.objects.first()
    itype = IssueType.objects.get(pk=payload.issue_type_id) if payload.issue_type_id else IssueType.objects.first()
    issue = Issue.objects.create(
        project=project, reporter=request.user, summary=payload.summary,
        description=payload.description, status=status, priority=priority, issue_type=itype,
        assignee_id=payload.assignee_id, sprint_id=payload.sprint_id,
        story_points=payload.story_points, due_date=payload.due_date,
    )
    return IssueOut.from_issue(issue)


@api.get("/issues/{key}/", response=IssueOut)
def get_issue(request, key: str):
    issue = get_object_or_404(
        Issue.objects.select_related("status", "priority", "issue_type", "assignee", "reporter", "project"),
        key=key,
    )
    return IssueOut.from_issue(issue)


@api.patch("/issues/{key}/", response=IssueOut)
def patch_issue(request, key: str, payload: IssuePatch):
    issue = get_object_or_404(Issue, key=key)
    data = payload.dict(exclude_unset=True)
    for field, value in data.items():
        setattr(issue, field, value)
    issue.save()
    issue.refresh_from_db()
    return IssueOut.from_issue(issue)


@api.delete("/issues/{key}/")
def delete_issue(request, key: str):
    issue = get_object_or_404(Issue, key=key)
    issue.delete()
    return {"deleted": key}


@api.get("/issues/{key}/comments/", response=Page[CommentOut])
def list_comments(request, key: str, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    issue = get_object_or_404(Issue, key=key)
    qs = issue.comments.select_related("author").order_by("created_at")
    return paginate(qs, CommentOut.from_comment, page, size)


@api.post("/issues/{key}/comments/", response=CommentOut)
def add_comment(request, key: str, payload: CommentIn):
    issue = get_object_or_404(Issue, key=key)
    c = Comment.objects.create(issue=issue, author=request.user, body=payload.body)
    return CommentOut.from_comment(c)


@api.get("/me/", response=UserOut)
def me(request):
    return request.user
