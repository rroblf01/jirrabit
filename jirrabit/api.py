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
import inspect
from datetime import date as _date

from django.db import models
from django.http import Http404
from django.utils import timezone
from ninja import ModelSchema, NinjaAPI, Schema
from ninja.security import HttpBearer, django_auth

from accounts.models import APIKey, User
from issues.models import Comment, Issue, IssueLink, IssueType, Priority, Status, WorkLog
from projects.models import Project, SavedFilter, Sprint


async def _visible_project(request, key: str) -> Project:
    """Return ``Project`` by key only if the requesting user can see it.

    Wraps ``filter_visible`` so non-members get 404 (rather than 403,
    avoiding the leak of which project keys exist).
    """
    qs = Project.objects.filter_visible(request.user)
    try:
        return await qs.aget(key=key)
    except Project.DoesNotExist as exc:
        raise Http404 from exc


async def _visible_issue(request, key: str) -> Issue:
    """Same as ``_visible_project`` at the issue level."""
    visible = Project.objects.filter_visible(request.user)
    # status__allowed_next is prefetched so that Status.can_transition_to works
    # in memory: it calls .allowed_next.all(), which without the prefetch is a
    # synchronous query and raises SynchronousOnlyOperation from an async view.
    # Prefetching it costs nothing extra — the rows are needed either way.
    qs = Issue.objects.filter(project__in=visible).select_related(
        "project", "status", "priority", "issue_type", "assignee", "reporter", "parent"
    ).prefetch_related("labels", "status__allowed_next")
    try:
        return await qs.aget(key=key)
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
    # Order matters: django-ninja tries the callbacks in sequence and stops at
    # the first success. ``django_auth`` subclasses APIKeyCookie, which enforces
    # a CSRF token on every unsafe method — correct for a cookie-authenticated
    # browser session, but it made every POST/PATCH/DELETE from a Bearer-token
    # client fail with "CSRF check Failed" before ``APIKeyAuth`` was ever
    # consulted. Listing the bearer handler first means token clients never
    # reach the CSRF check (there is no cookie for CSRF to protect), while
    # cookie sessions still do and keep their protection.
    auth=[APIKeyAuth(), django_auth],
)


# --- pagination helpers --------------------------------------------------

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

class Page[T](Schema):
    """Generic list-envelope returned by list endpoints."""

    count: int
    page: int
    size: int
    pages: int
    next: int | None = None
    previous: int | None = None
    items: list[T]


async def paginate(queryset, builder, page: int, size: int) -> dict:
    """Slice ``queryset`` and wrap into a ``Page`` payload.

    ``builder(item)`` converts each row to the right schema. It may be sync or
    async, and the result is awaited only when it is awaitable: django-ninja
    generates ``from_orm`` as a plain classmethod, so the ModelSchema builders
    cannot be made async, while ``IssueOut.afrom_issue`` has to be because
    serialising an issue reads the labels and the parent.

    The count and the iteration are unconditionally async: both block, and this
    file runs on the event loop, where the sync calls would raise
    ``SynchronousOnlyOperation``.
    """
    page = max(int(page or 1), 1)
    size = max(1, min(int(size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    total = await queryset.acount()
    offset = (page - 1) * size
    # Slicing stays lazy; only the iteration has to be async.
    items = []
    async for row in queryset[offset : offset + size]:
        built = builder(row)
        items.append(await built if inspect.isawaitable(built) else built)
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


class UserSearchOut(ModelSchema):
    """User fields safe to expose to any authenticated caller.

    Deliberately excludes ``is_active`` and ``is_staff``: the search endpoint is
    how a client resolves an assignee, and nothing about privilege belongs in
    that answer.
    """

    class Meta:
        model = User
        fields = ["id", "username", "display_name", "email"]


class IssueTypeOut(ModelSchema):
    """Issue type metadata.

    jirrabit's issue types are global rather than per-project, so this is a flat
    list rather than scoped to a project key.
    """

    class Meta:
        model = IssueType
        fields = ["id", "name", "category", "icon", "color"]


class StatusOut(ModelSchema):
    """Status metadata, including the category that groups it as todo /
    in-progress / done. Clients use the category to reason about progress
    without string-matching on the status name."""

    class Meta:
        model = Status
        fields = ["id", "name", "category", "order", "wip_limit"]


class PriorityOut(ModelSchema):
    class Meta:
        model = Priority
        fields = ["id", "name", "weight", "color"]


class ProjectOut(ModelSchema):
    class Meta:
        model = Project
        fields = ["id", "key", "name", "description", "archived"]


class SprintOut(ModelSchema):
    class Meta:
        model = Sprint
        fields = ["id", "name", "goal", "status", "start_date", "end_date", "retro_notes"]


class SprintIn(Schema):
    name: str | None = None
    goal: str | None = None
    start_date: _date | None = None
    end_date: _date | None = None
    retro_notes: str | None = None


class ProjectIn(Schema):
    name: str | None = None
    description: str | None = None
    archived: bool | None = None


class WorkLogOut(Schema):
    id: int
    issue: str
    author: str
    minutes: int
    comment: str
    logged_at: str

    @staticmethod
    def from_log(w: WorkLog) -> WorkLogOut:
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
    assignee: str | None = None
    reporter: str | None = None
    story_points: int | None = None
    due_date: _date | None = None
    estimate_minutes: int | None = None
    time_spent_minutes: int = 0

    # The fields below are additions rather than replacements: a client that
    # only knew the fields above keeps working, and one that needs ids or
    # categories no longer has to infer them from a display name.

    #: Numeric ids, for clients that address records by id.
    status_id: int = 0
    priority_id: int = 0
    issue_type_id: int = 0

    #: ``Status.category`` — todo / in_progress / done. Grouping by name would
    #: break the moment a project renames a status.
    status_category: str = ""

    created: str = ""
    updated: str = ""
    labels: list[str] = []
    #: Key of the parent issue when this is a subtask.
    parent: str = ""
    sprint_id: int | None = None

    @staticmethod
    async def afrom_issue(i: Issue) -> IssueOut:
        """Build the payload for ``i``.

        Async because serialising an issue touches the database twice: the
        labels come from a many-to-many join, and the parent key needs the
        parent loaded. Both are ordinary attribute reads that would fire a query
        and raise ``SynchronousOnlyOperation`` on the event loop.

        ``status``, ``priority``, ``issue_type``, ``assignee``, ``reporter`` and
        ``parent`` must already be select_related by the caller. Rather than
        hiding a stray query behind a lazy refresh, the access is guarded so a
        missing select_related shows up as a blank field instead of a
        synchronous database call in the middle of an async request.
        """
        labels = [label.name async for label in i.labels.all()]
        return IssueOut(
            id=i.pk,
            key=i.key,
            summary=i.summary,
            description=i.description,
            status=str(i.status) if i.status_id else "",
            priority=str(i.priority) if i.priority_id else "",
            type=str(i.issue_type) if i.issue_type_id else "",
            project=i.project.key if i.project_id else "",
            assignee=getattr(i.assignee, "username", None),
            reporter=getattr(i.reporter, "username", None),
            story_points=i.story_points,
            due_date=i.due_date,
            estimate_minutes=i.estimate_minutes,
            time_spent_minutes=i.time_spent_minutes,
            status_id=i.status_id or 0,
            status_category=i.status.category if i.status_id else "",
            priority_id=i.priority_id or 0,
            issue_type_id=i.issue_type_id or 0,
            created=i.created_at.isoformat() if i.created_at else "",
            updated=i.updated_at.isoformat() if i.updated_at else "",
            labels=labels,
            parent=i.parent.key if i.parent_id else "",
            sprint_id=i.sprint_id,
        )


class IssueIn(Schema):
    summary: str
    description: str = ""
    issue_type_id: int | None = None
    status_id: int | None = None
    priority_id: int | None = None
    assignee_id: int | None = None
    sprint_id: int | None = None
    story_points: int | None = None
    due_date: _date | None = None


class IssuePatch(Schema):
    summary: str | None = None
    description: str | None = None
    status_id: int | None = None
    priority_id: int | None = None
    assignee_id: int | None = None
    sprint_id: int | None = None
    story_points: int | None = None
    due_date: _date | None = None


class CommentOut(Schema):
    id: int
    issue: str
    author: str
    body: str
    created_at: str
    edited: bool

    @staticmethod
    def from_comment(c: Comment) -> CommentOut:
        return CommentOut(
            id=c.pk, issue=c.issue.key, author=str(c.author),
            body=c.body, created_at=c.created_at.isoformat(), edited=c.edited,
        )


class CommentIn(Schema):
    body: str


class IssueLinkIn(Schema):
    """A link between two issues.

    ``link_type`` accepts the model's stored keys (``blocks``, ``blocked_by``,
    ``relates_to``, ``duplicates``, ``duplicated_by``) as well as Jira's display
    spellings, because callers trained on Jira write the latter.
    """

    link_type: str
    #: The other end of the link. A link is directional, so the field naming
    #: says which side this issue is on rather than being a flat "other_issue".
    inward_issue_key: str
    outward_issue_key: str
    comment: str = ""


# --- endpoints -----------------------------------------------------------

@api.get("/projects/", response=Page[ProjectOut])
async def list_projects(request, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    qs = Project.objects.filter_visible(request.user).order_by("key")
    return await paginate(qs, lambda p: ProjectOut.from_orm(p), page, size)


# --- metadata -------------------------------------------------------------
# These endpoints exist so a client can turn human input into the numeric ids
# the write endpoints take, and so a client can read the workflow vocabulary
# without hardcoding it. They are deliberately read-only and global: jirrabit's
# statuses, priorities and issue types are not scoped per project.


@api.get("/issue-types/", response=Page[IssueTypeOut])
async def list_issue_types(request, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    qs = IssueType.objects.order_by("id")
    return await paginate(qs, lambda t: IssueTypeOut.from_orm(t), page, size)


@api.get("/statuses/", response=Page[StatusOut])
async def list_statuses(request, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    qs = Status.objects.order_by("order", "id")
    return await paginate(qs, lambda s: StatusOut.from_orm(s), page, size)


@api.get("/priorities/", response=Page[PriorityOut])
async def list_priorities(request, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    qs = Priority.objects.order_by("weight", "id")
    return await paginate(qs, lambda p: PriorityOut.from_orm(p), page, size)


@api.get("/users/search/", response=Page[UserSearchOut])
async def search_users(request, query: str = "", page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    """Search users by username, display name or email.

    Results are not filtered by project membership: a client that needs only
    eligible assignees should combine this with the project key it already has.
    The empty query lists everyone, which is what a client wants when building
    a picker rather than resolving one specific name.
    """
    qs = User.objects.filter(is_active=True).order_by("username")
    term = query.strip()
    if term:
        qs = qs.filter(
            models.Q(username__icontains=term)
            | models.Q(display_name__icontains=term)
            | models.Q(email__icontains=term)
            | models.Q(first_name__icontains=term)
            | models.Q(last_name__icontains=term)
        )
    return await paginate(qs, lambda u: UserSearchOut.from_orm(u), page, size)


@api.get("/users/{user_id}/", response=UserOut)
async def get_user(request, user_id: int):
    from django.http import Http404

    try:
        return await User.objects.aget(pk=user_id, is_active=True)
    except User.DoesNotExist as exc:
        raise Http404 from exc


@api.get("/projects/{key}/", response=ProjectOut)
async def get_project(request, key: str):
    return await _visible_project(request, key)


@api.get("/projects/{key}/sprints/", response=Page[SprintOut])
async def list_sprints(request, key: str, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    project = await _visible_project(request, key)
    return await paginate(project.sprints.all(), lambda s: SprintOut.from_orm(s), page, size)


@api.get("/projects/{key}/issues/", response=Page[IssueOut])
async def list_issues(
    request,
    key: str,
    page: int = 1,
    size: int = DEFAULT_PAGE_SIZE,
    status: str | None = None,
    assignee: str | None = None,
):
    project = await _visible_project(request, key)
    qs = project.issues.select_related(
        "status", "priority", "issue_type", "assignee", "reporter", "project", "parent"
    ).prefetch_related("labels", "status__allowed_next").order_by("-updated_at")
    if status:
        qs = qs.filter(status__name__iexact=status)
    if assignee:
        qs = qs.filter(assignee__username=assignee)
    return await paginate(qs, IssueOut.afrom_issue, page, size)


async def _validate_assignee(project, user_id):
    if user_id is None:
        return None
    in_project = await (
        User.objects.filter(pk=user_id, is_active=True)
        .filter(models.Q(memberships__project=project) | models.Q(led_projects=project))
        .aexists()
    )
    if not in_project:
        from ninja.errors import HttpError
        raise HttpError(400, "assignee no pertenece al proyecto")
    return user_id


async def _validate_sprint(project, sprint_id):
    if sprint_id is None:
        return None
    if not await Sprint.objects.filter(pk=sprint_id, project=project).aexists():
        from ninja.errors import HttpError
        raise HttpError(400, "sprint no pertenece al proyecto")
    return sprint_id


@api.post("/projects/{key}/issues/", response=IssueOut)
async def create_issue(request, key: str, payload: IssueIn):
    from ninja.errors import HttpError
    project = await _visible_project(request, key)
    try:
        status = (
            await Status.objects.aget(pk=payload.status_id) if payload.status_id
            else await Status.objects.order_by("order").afirst()
        )
        priority = (
            await Priority.objects.aget(pk=payload.priority_id) if payload.priority_id
            else await Priority.objects.afirst()
        )
        itype = (
            await IssueType.objects.aget(pk=payload.issue_type_id) if payload.issue_type_id
            else await IssueType.objects.afirst()
        )
    except (Status.DoesNotExist, Priority.DoesNotExist, IssueType.DoesNotExist) as exc:
        raise HttpError(400, "status/priority/type inválido") from exc
    assignee_id = await _validate_assignee(project, payload.assignee_id)
    sprint_id = await _validate_sprint(project, payload.sprint_id)
    issue = await Issue.objects.acreate(
        project=project, reporter=request.user, summary=payload.summary,
        description=payload.description, status=status, priority=priority, issue_type=itype,
        assignee_id=assignee_id, sprint_id=sprint_id,
        story_points=payload.story_points, due_date=payload.due_date,
    )
    return await IssueOut.afrom_issue(issue)


@api.get("/issues/{key}/", response=IssueOut)
async def get_issue(request, key: str):
    issue = await _visible_issue(request, key)
    return await IssueOut.afrom_issue(issue)


_PATCHABLE_FIELDS = {
    "summary", "description", "status_id", "priority_id",
    "assignee_id", "sprint_id", "story_points", "due_date",
}


@api.patch("/issues/{key}/", response=IssueOut)
async def patch_issue(request, key: str, payload: IssuePatch):
    from ninja.errors import HttpError

    args_issue_key = key
    issue = await _visible_issue(request, key)
    data = payload.dict(exclude_unset=True)
    if "status_id" in data:
        if not await Status.objects.filter(pk=data["status_id"]).aexists():
            raise HttpError(400, "status inválido")
    if "priority_id" in data:
        if not await Priority.objects.filter(pk=data["priority_id"]).aexists():
            raise HttpError(400, "priority inválido")
    if "assignee_id" in data:
        await _validate_assignee(issue.project, data["assignee_id"])
    if "sprint_id" in data:
        await _validate_sprint(issue.project, data["sprint_id"])

    # A status change goes through the workflow chokepoint rather than a plain
    # assignment. _change_status_atomic validates the transition, takes the row
    # lock, maintains resolved_at and writes the HistoryEntry — none of which a
    # setattr + save() does. Importing it across apps mirrors board.views, which
    # already does the same.
    #
    # The check runs twice on purpose. The pre-check below rejects an illegal
    # transition with a 400 *before* anything is written, so a PATCH carrying
    # both a summary and a bad status does not half-apply. The authoritative
    # check still happens inside the atomic block, because the workflow may have
    # changed between the two.
    new_status_id = data.get("status_id")
    if new_status_id is not None and new_status_id != issue.status_id:
        target_status = await Status.objects.prefetch_related("allowed_next").aget(
            pk=new_status_id
        )
        if not issue.status.can_transition_to(target_status):
            raise HttpError(400, "transición de estado no permitida por el workflow")

    # status_id is applied separately below, so drop it from the generic loop.
    data.pop("status_id", None)
    for field, value in data.items():
        if field not in _PATCHABLE_FIELDS:
            continue
        setattr(issue, field, value)
    await issue.asave()

    if new_status_id is not None and new_status_id != issue.status_id:
        from asgiref.sync import sync_to_async

        from issues.views import _change_status_atomic

        # sync_to_async, like issues.views and board.views already do for this
        # function. It holds a SELECT ... FOR UPDATE inside a transaction, and a
        # transaction has to be one unbroken block: an async rewrite would
        # release the row lock at every await. thread_sensitive keeps it on the
        # same thread as the rest of the request's database work.
        _, _, allowed = await sync_to_async(
            _change_status_atomic, thread_sensitive=True
        )(issue.pk, new_status_id, request.user.pk)
        if not allowed:
            raise HttpError(400, "transición de estado no permitida por el workflow")
        # Re-read the row rather than calling arefresh_from_db: the in-memory
        # copy predates the transition, so a later save() from it would write
        # the old status back over the new one, and arefresh_from_db drops the
        # prefetch cache, leaving status and issue_type unloaded and turning
        # serialisation into a synchronous query.
        issue = await _visible_issue(request, args_issue_key)

    return await IssueOut.afrom_issue(issue)


@api.delete("/issues/{key}/")
async def delete_issue(request, key: str):
    issue = await _visible_issue(request, key)
    await issue.adelete()
    return {"deleted": key}


@api.get("/issues/{key}/comments/", response=Page[CommentOut])
async def list_comments(request, key: str, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    issue = await _visible_issue(request, key)
    qs = issue.comments.select_related("author").order_by("created_at")
    return await paginate(qs, CommentOut.from_comment, page, size)


@api.post("/issues/{key}/comments/", response=CommentOut)
async def add_comment(request, key: str, payload: CommentIn):
    issue = await _visible_issue(request, key)
    c = await Comment.objects.acreate(issue=issue, author=request.user, body=payload.body)
    return CommentOut.from_comment(c)


@api.get("/me/", response=UserOut)
async def me(request):
    return request.user


# --- project mgmt ---

_PROJECT_PATCHABLE = {"name", "description", "archived"}


async def _assert_project_admin(request, project: Project) -> None:
    """Raise ninja HttpError(403) if the user isn't admin/lead on the project."""
    from ninja.errors import HttpError
    if request.user.is_superuser or project.lead_id == request.user.pk:
        return
    from projects.models import ProjectMembership
    is_admin = await ProjectMembership.objects.filter(
        project=project, user=request.user, role="admin",
    ).aexists()
    if not is_admin:
        raise HttpError(403, "Requiere rol admin en el proyecto")


@api.patch("/projects/{key}/", response=ProjectOut)
async def patch_project(request, key: str, payload: ProjectIn):
    project = await _visible_project(request, key)
    await _assert_project_admin(request, project)
    data = payload.dict(exclude_unset=True)
    for field, value in data.items():
        if field in _PROJECT_PATCHABLE:
            setattr(project, field, value)
    await project.asave()
    return project


@api.delete("/projects/{key}/")
async def delete_project(request, key: str):
    project = await _visible_project(request, key)
    await _assert_project_admin(request, project)
    await project.adelete()
    return {"deleted": key}


# --- sprint mgmt ---

@api.get("/sprints/{sprint_id}/", response=SprintOut)
async def get_sprint(request, sprint_id: int):
    from django.http import Http404
    visible = Project.objects.filter_visible(request.user)
    try:
        return await Sprint.objects.select_related("project").aget(
            pk=sprint_id, project__in=visible,
        )
    except Sprint.DoesNotExist as exc:
        raise Http404 from exc


@api.patch("/sprints/{sprint_id}/", response=SprintOut)
async def patch_sprint(request, sprint_id: int, payload: SprintIn):
    from django.http import Http404
    try:
        sprint = await Sprint.objects.select_related("project").aget(pk=sprint_id)
    except Sprint.DoesNotExist as exc:
        raise Http404 from exc
    if not await Project.objects.filter_visible(request.user).filter(pk=sprint.project_id).aexists():
        raise Http404
    await _assert_project_admin(request, sprint.project)
    for field, value in payload.dict(exclude_unset=True).items():
        setattr(sprint, field, value)
    await sprint.asave()
    return sprint


@api.delete("/sprints/{sprint_id}/")
async def delete_sprint(request, sprint_id: int):
    from django.http import Http404
    try:
        sprint = await Sprint.objects.select_related("project").aget(pk=sprint_id)
    except Sprint.DoesNotExist as exc:
        raise Http404 from exc
    await _assert_project_admin(request, sprint.project)
    await sprint.adelete()
    return {"deleted": sprint_id}


# --- worklogs ---

@api.get("/issues/{key}/worklogs/", response=Page[WorkLogOut])
async def list_worklogs(request, key: str, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    issue = await _visible_issue(request, key)
    qs = issue.worklogs.select_related("author").order_by("-logged_at")
    return await paginate(qs, WorkLogOut.from_log, page, size)


@api.post("/issues/{key}/worklogs/", response=WorkLogOut)
async def add_worklog(request, key: str, payload: WorkLogIn):
    from ninja.errors import HttpError
    issue = await _visible_issue(request, key)
    if payload.minutes <= 0:
        raise HttpError(400, "minutes debe ser > 0")
    from asgiref.sync import sync_to_async

    from issues.views import _log_work_atomic

    # The whole write goes through issues.views._log_work_atomic rather than
    # being inlined here, for two reasons. It already holds the row lock and
    # maintains the time totals, and transaction.atomic() is not async-safe:
    # entering it from an async view raises SynchronousOnlyOperation. A
    # transaction also has to be one unbroken block, so the unit cannot be split
    # across awaits.
    _issue, log = await sync_to_async(_log_work_atomic, thread_sensitive=True)(
        issue.pk,
        request.user.pk,
        payload.minutes,
        payload.comment[:255],
    )
    return WorkLogOut.from_log(log)


# --- search ---------------------------------------------------------------
# Exposes search/jql.py over the API. The parser itself is unchanged and shared
# with the web UI's search page; only the transport is new.


@api.get("/search", response=Page[IssueOut])
async def search_issues(
    request,
    jql: str = "",
    page: int = 1,
    size: int = DEFAULT_PAGE_SIZE,
):
    from search.jql import JQLError, parse_jql

    try:
        condition, order = parse_jql(jql)
    except JQLError as exc:
        from ninja.errors import HttpError

        raise HttpError(400, str(exc)) from exc

    visible = Project.objects.filter_visible(request.user)
    qs = (
        Issue.objects.filter(condition, project__in=visible)
        .select_related(
            "project", "status", "priority", "issue_type", "assignee", "reporter", "parent"
        )
        .prefetch_related("labels", "status__allowed_next")
        .order_by(*(order or ["-updated_at"]))
        .distinct()
    )
    return await paginate(qs, IssueOut.afrom_issue, page, size)


# --- issue links ----------------------------------------------------------


#: Jira spells the link types with spaces and capitals; the model stores keys.
#: Both are accepted so a caller from either vocabulary lands.
_LINK_TYPE_ALIASES = {
    "blocks": "blocks",
    "block": "blocks",
    "is blocked by": "blocked_by",
    "blocked by": "blocked_by",
    "blocked_by": "blocked_by",
    "relates": "relates_to",
    "relates to": "relates_to",
    "relates_to": "relates_to",
    "duplicates": "duplicates",
    "duplicate of": "duplicates",
    "is duplicated by": "duplicated_by",
    "duplicated by": "duplicated_by",
    "duplicated_by": "duplicated_by",
}


class LinkTypeOut(ModelSchema):
    class Meta:
        model = IssueLink
        fields = ["id", "type", "source", "target", "created_by", "created_at"]


@api.get("/link-types/", response=list[str])
async def list_link_types(request):
    """The link types this instance supports, in the model's key spelling."""
    return [choice[0] for choice in IssueLink.TYPE_CHOICES]


@api.get("/issues/{key}/links/", response=list[LinkTypeOut])
async def list_issue_links(request, key: str):
    issue = await _visible_issue(request, key)
    outward = [link async for link in issue.links_out.all()]
    inward = [link async for link in issue.links_in.all()]
    return [*outward, *inward]


@api.post("/issues/{key}/links/", response=LinkTypeOut)
async def create_issue_link(request, key: str, payload: IssueLinkIn):
    from ninja.errors import HttpError

    issue = await _visible_issue(request, key)
    link_type = _LINK_TYPE_ALIASES.get(payload.link_type.strip().lower())
    if link_type is None:
        valid = ", ".join(choice[0] for choice in IssueLink.TYPE_CHOICES)
        raise HttpError(400, f"tipo de link desconocido: '{payload.link_type}'. Válidos: {valid}")

    # Both ends are resolved through the same visibility gate, so a link cannot
    # be used to reference an issue the caller is not allowed to see.
    target = await _visible_issue(request, payload.inward_issue_key)
    if payload.outward_issue_key != key:
        raise HttpError(400, "outward_issue_key debe ser el issue de la URL")

    if await IssueLink.objects.filter(source=issue, target=target, type=link_type).aexists():
        raise HttpError(400, "ese link ya existe")

    return await IssueLink.objects.acreate(
        source=issue, target=target, type=link_type, created_by=request.user
    )


# --- watchers -------------------------------------------------------------


@api.get("/issues/{key}/watchers/", response=list[str])
async def list_watchers(request, key: str):
    issue = await _visible_issue(request, key)
    return [u.username async for u in issue.watchers.order_by("username")]


@api.post("/issues/{key}/watchers/", response=list[str])
async def watch_issue(request, key: str):
    """Watch an issue. Idempotent: watching twice is not an error."""
    issue = await _visible_issue(request, key)
    await issue.watchers.aadd(request.user)
    return [u.username async for u in issue.watchers.order_by("username")]


@api.delete("/issues/{key}/watchers/", response=list[str])
async def unwatch_issue(request, key: str):
    issue = await _visible_issue(request, key)
    await issue.watchers.aremove(request.user)
    return [u.username async for u in issue.watchers.order_by("username")]


# --- saved filters --------------------------------------------------------
# SavedFilter is scoped to an owner rather than to a project, so there is one
# endpoint for all of them instead of one per project key.


class SavedFilterOut(ModelSchema):
    class Meta:
        model = SavedFilter
        fields = ["id", "name", "query", "scope", "created_at"]


@api.get("/filters/", response=list[SavedFilterOut])
async def list_saved_filters(request):
    visible = Project.objects.filter_visible(request.user)
    # A filter is visible if it belongs to the caller or was shared, and it must
    # not be able to reach projects the caller cannot see: filters whose JQL
    # references only hidden projects are dropped.
    qs = SavedFilter.objects.filter(
        models.Q(owner=request.user) | models.Q(scope="shared")
    ).order_by("name")
    # No avalues_list exists on QuerySet, so fetch the rows and read the key
    # in Python rather than projecting in SQL.
    visible_keys = {p.key async for p in visible}
    kept = []
    async for saved in qs:
        referenced = {
            chunk.split("=", 1)[-1].strip().strip("\"'")
            for chunk in saved.query.split("AND")
            if chunk.strip().lower().startswith("project")
        }
        referenced = {key for key in referenced if key}
        if not referenced or referenced <= visible_keys:
            kept.append(saved)
    return kept
