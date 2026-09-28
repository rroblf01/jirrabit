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
import re
from datetime import date as _date
from datetime import datetime
from typing import Any

from django.db import models
from django.http import Http404
from django.utils import timezone
from ninja import Field, ModelSchema, NinjaAPI, Schema
from ninja.security import HttpBearer, django_auth

from accounts.models import APIKey, Notification, Team, User
from issues.models import (
    Comment,
    Issue,
    IssueLink,
    IssueType,
    Label,
    Pin,
    Priority,
    Status,
    WorkLog,
)
from projects.models import Epic, Project, SavedFilter, Sprint


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
    qs = (
        Issue.objects.filter(project__in=visible)
        .select_related(
            "project", "status", "priority", "issue_type", "assignee", "reporter", "parent", "epic"
        )
        .prefetch_related("labels", "status__allowed_next")
    )
    try:
        return await qs.aget(key=key)
    except Issue.DoesNotExist as exc:
        raise Http404 from exc


class APIKeyAuth(HttpBearer):
    """Bearer token auth backed by ``accounts.APIKey``.

    Deliberately synchronous, and the only sync method left in this file:
    django-ninja calls ``authenticate`` outside the event loop, so a coroutine
    here would never be awaited. That is also why the ``last_used_at`` write
    below uses the sync ORM — it is the one place the rule cannot be applied.
    """

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
            id=w.pk,
            issue=w.issue.key,
            author=str(w.author),
            minutes=w.minutes,
            comment=w.comment,
            logged_at=w.logged_at.isoformat(),
        )


class WorkLogIn(Schema):
    minutes: int
    comment: str = ""
    # ISO 8601 timestamp for when the work happened. Omitted means now, which
    # is what the field has always meant: logged_at is auto_now_add, and the
    # only thing the create form ever recorded was the moment of the click.
    started: str | None = None


def _parse_started(value: str) -> datetime:
    """Parse a worklog timestamp into an aware datetime.

    Naive input is read in the server's default timezone rather than rejected:
    a caller that sends "2026-09-20T14:00:00" means their wall clock, and there
    is no wall clock on the wire to recover. Future timestamps are refused — a
    worklog is a record of time spent, not time planned.
    """
    from ninja.errors import HttpError

    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError, TypeError:
        raise HttpError(400, f"fecha inválida: {value!r}; usa ISO 8601") from None
    if parsed.tzinfo is None:
        parsed = timezone.make_aware(parsed)
    if parsed > timezone.now():
        raise HttpError(400, "no se puede registrar trabajo en el futuro")
    return parsed


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
    #: What is left of the estimate, in minutes. Read-only in the sense that the
    #: API recomputes it from logged time, so a caller should not try to keep the
    #: two in step by hand.
    time_remaining_minutes: int | None = None

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
    #: Name of the epic this issue belongs to. JQL could filter on `epic` from
    #: the start while nothing could read or set it, so a search would find issues
    #: whose epic the caller had no way to learn.
    epic: str = ""
    epic_id: int = 0
    sprint_id: int | None = None
    #: True when the issue is archived. Archived issues are hidden from the
    #: default listings, which is what makes archiving a real alternative to
    #: deleting one — so a client has to be able to see the flag to trust it.
    archived: bool = False

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
            time_remaining_minutes=i.time_remaining_minutes,
            status_id=i.status_id or 0,
            status_category=i.status.category if i.status_id else "",
            priority_id=i.priority_id or 0,
            issue_type_id=i.issue_type_id or 0,
            created=i.created_at.isoformat() if i.created_at else "",
            updated=i.updated_at.isoformat() if i.updated_at else "",
            labels=labels,
            parent=i.parent.key if i.parent_id else "",
            epic=str(i.epic) if i.epic_id else "",
            epic_id=i.epic_id or 0,
            sprint_id=i.sprint_id,
            archived=i.archived,
        )


class IssueIn(Schema):
    summary: str
    description: str = ""
    issue_type_id: int | None = None
    status_id: int | None = None
    priority_id: int | None = None
    assignee_id: int | None = None
    sprint_id: int | None = None
    epic_id: int | None = None
    #: Key of the issue this is a subtask of. Validated to be a visible issue in
    #: the same project, so the parent chain cannot cross projects.
    parent: str | None = None
    #: Label names, created on demand. Names and not ids because a label is a
    #: word rather than a record, and no agent would look up an id for one.
    labels: list[str] | None = None
    story_points: int | None = None
    due_date: _date | None = None
    #: In minutes, like every other time field jirrabit keeps. The API is
    #: minutes all the way down; only the Jira-shaped output converts to seconds.
    estimate_minutes: int | None = None
    time_remaining_minutes: int | None = None


class IssuePatch(Schema):
    summary: str | None = None
    description: str | None = None
    status_id: int | None = None
    priority_id: int | None = None
    #: Declared here, which it was not for a long time, so a PATCH naming it was
    #: accepted and ignored: django-ninja drops a body key the schema does not
    #: declare, and _PATCHABLE_FIELDS did not list it either, so nothing anywhere
    #: said no. Creating an issue with the same field always worked, which is what
    #: made the asymmetry so easy to miss.
    issue_type_id: int | None = None
    assignee_id: int | None = None
    sprint_id: int | None = None
    epic_id: int | None = None
    parent: str | None = None
    labels: list[str] | None = None
    story_points: int | None = None
    due_date: _date | None = None
    estimate_minutes: int | None = None
    time_remaining_minutes: int | None = None
    #: Archive and unarchive. Archiving is reversible and hides the issue from
    #: the default listings, so it is the operation to reach for instead of
    #: deleting. It is a plain PATCH field rather than its own endpoint because
    #: it needs no body and no confirmation of its own.
    archived: bool | None = None


class CommentOut(Schema):
    id: int
    issue: str
    author: str
    body: str
    created_at: str
    edited: bool
    #: Set when the comment was soft-deleted. The body is kept so a restore can
    #: put it back; the default listing filters these out, and this flag is what
    #: tells a client the difference between "never existed" and "removed".
    deleted_at: str | None = None

    @staticmethod
    def from_comment(c: Comment) -> CommentOut:
        return CommentOut(
            id=c.pk,
            issue=c.issue.key,
            author=str(c.author),
            body=c.body,
            created_at=c.created_at.isoformat(),
            edited=c.edited,
            deleted_at=c.deleted_at.isoformat() if c.deleted_at else None,
        )


class CommentIn(Schema):
    body: str


class CommentEditOut(Schema):
    """One previous version of a comment body, from ``CommentEdit``."""

    id: int
    old_body: str
    edited_at: str
    edited_by: str = ""

    @staticmethod
    def from_edit(e) -> CommentEditOut:
        return CommentEditOut(
            id=e.pk,
            old_body=e.old_body,
            edited_at=e.edited_at.isoformat() if e.edited_at else "",
            edited_by=e.edited_by.username if e.edited_by_id else "",
        )


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
    archived: bool = False,
):
    project = await _visible_project(request, key)
    qs = (
        project.issues.select_related(
            "status", "priority", "issue_type", "assignee", "reporter", "project", "parent", "epic"
        )
        .prefetch_related("labels", "status__allowed_next")
        .order_by("-updated_at")
    )
    # Archived issues are hidden unless asked for, which is what makes archiving
    # a real alternative to deleting. `?archived=1` reveals them, mirroring the
    # override the board has always had, so a client can list only the archived
    # ones by combining it with the status filter.
    if not archived:
        qs = qs.filter(archived=False)
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


#: Distinguishes "the caller did not mention this field" from "the caller set it
#: to null". Every nullable field here has a meaningful null — unassign, clear the
#: epic, detach the parent — so `None` cannot double as "absent", and
#: exclude_unset alone is not enough once a value has to survive a pop().
_UNSET = object()


async def _resolve_issue_key(project, key, *, exclude=None):
    """Resolve an issue key to its pk, refusing anything outside the project.

    Keys rather than ids on the way in, to match every other endpoint in this
    file and because a key is the only handle a client has on an issue.

    No visibility check of its own is needed, and that is worth being explicit
    about: the caller passed the project through ``_visible_project``, and
    visibility in jirrabit is per project, so any issue in a visible project is
    visible. What is checked is membership of the project, which is a 404 rather
    than a 400 for the same reason as everywhere else — a validation failure
    would confirm that the key exists.
    """
    key = (key or "").strip()
    if not key:
        return None
    qs = Issue.objects.filter(key=key, project=project)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    found = await qs.only("pk").afirst()
    if found is None:
        raise Http404
    return found.pk


async def _validate_sprint(project, sprint_id):
    if sprint_id is None:
        return None
    if not await Sprint.objects.filter(pk=sprint_id, project=project).aexists():
        from ninja.errors import HttpError

        raise HttpError(400, "sprint no pertenece al proyecto")
    return sprint_id


async def _validate_epic(project, epic_id):
    """An epic must belong to the same project, or the link crosses projects."""
    if epic_id is None:
        return None
    if not await Epic.objects.filter(pk=epic_id, project=project).aexists():
        from ninja.errors import HttpError

        raise HttpError(400, "epic no pertenece al proyecto")
    return epic_id


async def _validate_labels(names):
    """Resolve label names to ids, creating the ones that do not exist.

    Name-based because that is how every other client-facing surface handles
    labels and how a person thinks of them; a label is a bare word, not a record
    with an id anybody remembers. Creating on demand is what the web UI does too,
    and the alternative is a client that has to call the label endpoint before it
    can set one, which no Atlassian-trained agent would do.
    """
    from issues.models import Label

    resolved = []
    for raw in names:
        name = str(raw).strip()
        if not name:
            continue
        label, _created = await Label.objects.aget_or_create(name=name)
        resolved.append(label)
    return resolved


class LabelOut(ModelSchema):
    class Meta:
        model = Label
        fields = ["id", "name", "color"]


# A note on the two ways a field can look like it works here. A body key this
# API's schema does not declare is dropped, not refused — django-ninja ignores it,
# so PATCH /issues/{key}/ with an undeclared field answers 200 and changes
# nothing. `issue_type_id` was missing from IssuePatch for a long time, which is
# exactly that. And a key the schema does declare can still be dropped: a field
# left out of _PATCHABLE_FIELDS is applied to nothing. Both happened to the same
# field, which is why a field can look broken while creating an issue with it
# works perfectly.
#
# --- board placement, pins, timers, reactions, branches, templates ---
#
# The features that exist in the web UI and had no HTTP path at all. They are
# small individually and none of them is central, which is exactly why they were
# all left: the UI grew them one at a time and the API was not part of the loop.
#
# Two of them touch the board's ordering invariant, so they are written against
# the same helpers the drag path uses rather than assigning a rank directly.


class MoveIn(Schema):
    #: Where to go. Omit it to stay in the same column and only change position,
    #: which is what a reorder without a move means.
    status_id: int | None = None
    #: 0-based index within the destination column. Absent means the end, which is
    #: what a status change without a position means everywhere else in jirrabit.
    rank: float | None = None


@api.post("/issues/{key}/move/", response=IssueOut)
async def move_issue(request, key: str, payload: MoveIn):
    """Put an issue in a status, and optionally at a rank within its column.

    This is the write the board's drag performs, and it goes through the same two
    helpers — the status chokepoint and the column appender — so a move from the
    API renumbers the column it leaves and notifies watchers exactly as a drag
    does. Assigning a rank directly would break the dense ``0..n-1`` invariant the
    board depends on.
    """
    from asgiref.sync import sync_to_async

    from board.views import _append_to_column, _apply_board_order, _densify_after_delete
    from issues.views import _change_status_atomic

    issue = await _visible_issue(request, key)
    await _assert_can_edit(request, issue.project)
    from ninja.errors import HttpError

    if payload.status_id is None:
        target, source_id = issue.status, None
    else:
        if not await Status.objects.filter(pk=payload.status_id).aexists():
            raise HttpError(400, "status inválido")
        target = await Status.objects.prefetch_related("allowed_next").aget(pk=payload.status_id)
        source_id = issue.status_id
        if not issue.status.can_transition_to(target):
            raise HttpError(400, "transición de estado no permitida por el workflow")

    if target.pk == issue.status_id and payload.rank is None:
        return await IssueOut.afrom_issue(issue)

    project = issue.project
    if target.pk != issue.status_id:
        moved, _new, ok = await sync_to_async(_change_status_atomic, thread_sensitive=True)(
            issue.pk, target.pk, request.user.pk
        )
        if not ok:
            raise HttpError(400, "transición de estado no permitida por el workflow")
        # Appended, because a move without a position has nowhere else to put it.
        await _append_to_column([moved], target)

    if payload.rank is not None:
        # An explicit rank is honoured by renumbering the whole column, which is
        # the only way to insert at a position without a midpoint scheme — the
        # same reason the drag submits the entire column. Re-read first so the
        # card's own key is in the list, and so a status change made above is
        # visible.
        placed = await _visible_issue(request, key)
        rest = [
            i.key
            async for i in Issue.objects.filter(project=project, status=target)
            .exclude(pk=placed.pk)
            .order_by("rank", "-updated_at")
            .only("key")
        ]
        index = max(0, min(int(payload.rank), len(rest)))
        wanted = [placed.key] + rest
        wanted.insert(index, wanted.pop(0))
        await _apply_board_order(project, target, wanted, request.user)
    elif source_id is not None:
        # The departure left a hole in the column it left.
        await _densify_after_delete(project, [source_id])
    return await IssueOut.afrom_issue(await _visible_issue(request, key))


class BoardReorderIn(Schema):
    status_id: int
    #: The whole column as the board sees it. A complete list rather than a
    #: before/after pair, which is what makes the operation idempotent and avoids
    #: midpoint arithmetic on a FloatField.
    keys: list[str]


@api.post("/projects/{key}/board/reorder/", response=dict)
async def reorder_board(request, key: str, payload: BoardReorderIn):
    """Renumber one board column to a given order. Needs edit rights on the project.

    Takes the whole column, like the board's drag does. Two cards swapping places
    is then the same call as a column of twenty, and there is no arithmetic to get
    wrong.

    A *partial* list is legal and means "these to the top, the rest keep their
    order", because that is what a stale browser tab sends and refusing it would
    make a reorder impossible the moment someone else moved a card. A key that is
    not in this project is a 400; an illegal status change raises from the
    workflow check inside the helper.
    """
    from board.views import _apply_board_order

    project = await _visible_project(request, key)
    await _assert_can_edit(request, project)
    from ninja.errors import HttpError

    if not await Status.objects.filter(pk=payload.status_id).aexists():
        raise HttpError(400, "status inválido")
    try:
        # The rows come back as .only() instances, so `i.key` and nothing else:
        # Issue.__str__ reads summary and would be a synchronous query here.
        moved = await _apply_board_order(
            project,
            await Status.objects.aget(pk=payload.status_id),
            list(payload.keys),
            request.user,
        )
    except ValueError as exc:
        # Raised for a key outside the project, or a status change the workflow
        # forbids. Translated rather than left to propagate: a 500 for a bad
        # request is a dead end.
        raise HttpError(400, str(exc)) from exc
    return {
        "statusId": payload.status_id,
        "keys": [i.key for i in moved],
        "moved": len(moved),
    }


class BulkUpdateIn(Schema):
    #: Issue keys, not ids: a key is the only handle most clients have.
    keys: list[str]
    action: str
    value: str = ""


@api.post("/projects/{key}/board/bulk-update/", response=dict)
async def board_bulk_update(request, key: str, payload: BulkUpdateIn):
    """Apply one change to several issues at once. Edit rights on the project.

    Mirrors the board's own bulk bar, including the action names, so an agent and
    a person are speaking the same vocabulary. ``delete`` is refused here: it is
    the only action that cannot be undone, and it belongs behind the MCP's
    two-step confirmation rather than in a bulk call that takes a list.
    """
    from asgiref.sync import sync_to_async

    from issues.views import _change_status_atomic

    project = await _visible_project(request, key)
    await _assert_can_edit(request, project)
    from ninja.errors import HttpError

    actions = {"status", "assignee", "priority", "sprint", "epic", "label_add", "label_remove"}
    if payload.action == "delete":
        # The one action here that cannot be undone. It is deliberately not
        # reachable from a list call: the MCP deletes through a tool that shows
        # what is about to go and asks for a confirmation, and a bulk endpoint
        # that skipped that would be the way around it.
        raise HttpError(
            400,
            "borrar no está aquí: borra los issues uno a uno con deleteJiraIssue, "
            "que pide confirmación explícita",
        )
    if payload.action not in actions:
        raise HttpError(400, f"acción desconocida; usa una de {', '.join(sorted(actions))}")
    if payload.action == "status" and not await Status.objects.filter(pk=payload.value).aexists():
        raise HttpError(400, "status inválido")
    missing = [k for k in payload.keys if not await Issue.objects.filter(key=k, project=project).aexists()]
    if missing:
        raise HttpError(400, f"claves que no están en este proyecto: {sorted(missing)}")

    changed, skipped = [], []
    for issue_key in payload.keys:
        issue = await Issue.objects.filter(key=issue_key, project=project).select_related("project").afirst()
        if issue is None:  # checked above; here so a race is a skip, not a 500
            skipped.append(issue_key)
            continue
        if payload.action == "status":
            # Through the chokepoint, one issue at a time, exactly as the board
            # does it: a single illegal transition must not half-apply the rest.
            _moved, _new, ok = await sync_to_async(_change_status_atomic, thread_sensitive=True)(
                issue.pk, int(payload.value), request.user.pk
            )
            if ok:
                changed.append(issue_key)
            else:
                skipped.append(issue_key)
        else:
            if await _apply_bulk_field(issue, payload.action, payload.value, request.user):
                changed.append(issue_key)
            else:
                skipped.append(issue_key)
    result = {"changed": changed, "skipped": skipped}
    if skipped:
        result["note"] = f"{len(skipped)} no se pudieron cambiar; revisa los errores del board para el motivo"
    return result


async def _apply_bulk_field(issue: Issue, action: str, value: str, user) -> bool:
    """The non-status bulk actions. One issue at a time, each with its own save.

    A single aupdate would be one line and would skip all four post_save
    receivers, so no watcher would be notified and the audit log and every other
    browser's board would go stale — the trap AGENTS.md is explicit about.
    """
    from issues.models import Label

    if action == "assignee":
        target = await _validate_assignee(issue.project, int(value) if value else None)
        issue.assignee_id = target
    elif action == "priority":
        if not await Priority.objects.filter(pk=value).aexists():
            return False
        issue.priority_id = int(value)
    elif action == "sprint":
        issue.sprint_id = await _validate_sprint(issue.project, int(value) if value else None)
    elif action == "epic":
        issue.epic_id = await _validate_epic(issue.project, int(value) if value else None)
    elif action in {"label_add", "label_remove"}:
        label, _ = await Label.objects.aget_or_create(name=value.strip())
        current = {held.name async for held in issue.labels.all()}
        if action == "label_add":
            current.add(value.strip())
        else:
            current.discard(value.strip())
        await issue.labels.aset([found async for found in Label.objects.filter(name__in=current)])
        return True
    else:  # pragma: no cover - the caller checks the action name first
        return False
    await issue.asave()
    return True


# --- pins ---


class PinOut(Schema):
    id: int
    issue: str = ""
    project: str = ""
    created_at: str


async def _pin_out(pin) -> PinOut:
    """Reads ``pin.issue`` and ``pin.project``, so the row must be select_related'd.

    A Pin from ``aget_or_create`` on an *existing* row has neither relation
    cached — only the branch that creates one carries the object it was handed —
    and touching an uncached relation from an async view is a synchronous query.
    So the create path re-fetches rather than assuming.
    """
    return PinOut(
        id=pin.pk,
        issue=pin.issue.key if pin.issue_id else "",
        project=pin.project.key if pin.project_id else "",
        created_at=pin.created_at.isoformat() if pin.created_at else "",
    )


@api.get("/pins/", response=Page[PinOut])
async def list_pins(request, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    """The caller's own pins. Pinned issues and projects, newest first."""
    qs = Pin.objects.filter(user=request.user).select_related("issue", "project")
    return await paginate(qs, _pin_out, page, size)


class PinIn(Schema):
    issue: str | None = None
    project: str | None = None


@api.post("/pins/", response=PinOut)
async def create_pin(request, payload: PinIn):
    """Pin an issue or a project. Idempotent: pinning twice is not an error."""
    from ninja.errors import HttpError

    from issues.models import Pin

    if bool(payload.issue) == bool(payload.project):
        raise HttpError(400, "indica exactamente uno de issue o project: un pin es de una cosa o de otra")
    # One target, chosen by the check above; a type checker cannot see that the
    # else branch is the only way to get here without an issue, hence the cast.
    target: Any = (
        await _visible_issue(request, payload.issue)
        if payload.issue
        else await _visible_project(request, str(payload.project))
    )
    field = "issue" if payload.issue else "project"
    # Passed as one mapping rather than **kwargs because aget_or_create's first
    # parameter is `defaults`, and a checker matching `**` against it cannot tell
    # a key named `issue` from one named `defaults`.
    lookup: dict[str, Any] = {field: target}
    await Pin.objects.aget_or_create(user=request.user, **lookup)
    # Re-read with the relations rather than using what aget_or_create returned.
    pin = await Pin.objects.filter(user=request.user, **lookup).select_related("issue", "project").afirst()
    return await _pin_out(pin)


@api.delete("/pins/{pin_id}/", response=dict)
async def delete_pin(request, pin_id: int):
    from issues.models import Pin

    removed, _ = await Pin.objects.filter(pk=pin_id, user=request.user).adelete()
    if not removed:
        raise Http404
    return {"deleted": pin_id}


# --- timers ---
#
# Stopping a timer mints a WorkLog, so it is a write that produces time. It is
# the one bulk-safe path here because the WebLog write and the timer delete are
# two halves of one thing, and the totals go through issues.views._log_work_atomic.


class TimerOut(Schema):
    issue: str
    started_at: str
    running: bool = True


@api.get("/issues/{key}/timer/", response=TimerOut)
async def get_timer(request, key: str):
    """The caller's running timer on this issue. 404 when none is running."""
    from issues.models import Timer

    issue = await _visible_issue(request, key)
    timer = await Timer.objects.filter(user=request.user, issue=issue).afirst()
    if timer is None:
        raise Http404
    return TimerOut(issue=issue.key, started_at=timer.started_at.isoformat(), running=True)


@api.post("/issues/{key}/timer/start/", response=TimerOut)
async def start_timer(request, key: str):
    """Start timing work on an issue. One running timer per user.

    Starting while another is running returns 409 rather than moving it: a timer
    is the user's own, and silently restarting the one they are working on would
    lose the minutes already spent.
    """
    from ninja.errors import HttpError

    from issues.models import Timer

    issue = await _visible_issue(request, key)
    await _assert_can_edit(request, issue.project)
    running = await Timer.objects.filter(user=request.user).select_related("issue").afirst()
    if running is not None:
        raise HttpError(
            409,
            f"ya hay un temporizador en marcha sobre {running.issue.key}; paralo antes de empezar otro",
        )
    timer = await Timer.objects.acreate(user=request.user, issue=issue)
    return TimerOut(issue=issue.key, started_at=timer.started_at.isoformat(), running=True)


@api.post("/issues/{key}/timer/stop/", response=WorkLogOut)
async def stop_timer(request, key: str):
    from asgiref.sync import sync_to_async

    from issues.views import _log_work_atomic

    """Stop the timer and log the minutes it ran, as a worklog entry.

    Atomic: the worklog and the issue's time totals are written in one block, and
    the timer is removed in the same one. A timer that stopped without its time
    being recorded would be work that vanished.
    """
    from ninja.errors import HttpError

    from issues.models import Timer

    issue = await _visible_issue(request, key)
    await _assert_can_edit(request, issue.project)
    timer = await Timer.objects.filter(user=request.user, issue=issue).afirst()
    if timer is None:
        raise HttpError(404, "no hay ningún temporizador en marcha sobre esta tarea")
    started = timer.started_at
    minutes = max(1, int((timezone.now() - started).total_seconds() // 60))
    _issue, log = await sync_to_async(_log_work_atomic, thread_sensitive=True)(
        issue.pk, request.user.pk, minutes, "Timer"
    )
    # The timer goes after the worklog, and if the delete failed the user has a
    # worklog and a running timer rather than neither.
    await Timer.objects.filter(pk=timer.pk).adelete()
    return WorkLogOut.from_log(log)


# --- snooze ---


class SnoozeOut(Schema):
    issue: str
    until: str


class SnoozeIn(Schema):
    #: ISO-8601. How far ahead is up to the caller; the UI offers a fixed set of
    #: options and an agent has no such menu.
    until: str


@api.post("/issues/{key}/snooze/", response=SnoozeOut)
async def snooze_issue(request, key: str, payload: SnoozeIn):
    """Mute this issue's notifications for the caller until a time."""
    from ninja.errors import HttpError

    from issues.models import NotificationSnooze

    issue = await _visible_issue(request, key)
    try:
        until = datetime.fromisoformat(payload.until)
    except ValueError as exc:
        raise HttpError(400, "until no es una fecha ISO-8601 válida") from exc
    if timezone.is_naive(until):
        until = timezone.make_aware(until)
    if until <= timezone.now():
        raise HttpError(400, "until ya ha pasado")
    snooze, _created = await NotificationSnooze.objects.aupdate_or_create(
        user=request.user, issue=issue, defaults={"until": until}
    )
    return SnoozeOut(issue=issue.key, until=snooze.until.isoformat())


@api.delete("/issues/{key}/snooze/", response=dict)
async def unsnooze_issue(request, key: str):
    from issues.models import NotificationSnooze

    issue = await _visible_issue(request, key)
    removed, _ = await NotificationSnooze.objects.filter(user=request.user, issue=issue).adelete()
    if not removed:
        raise Http404
    return {"removed": key}


# --- reactions ---


class ReactionOut(Schema):
    """The reactions on one comment, as counts.

    Counts rather than a list of who: a reaction is a signal, and forty rows to
    draw three numbers is not a useful answer. ``mine`` carries the caller's own,
    which is the only part a client has to act on.
    """

    comment: int
    counts: dict[str, int] = {}
    mine: list[str] = []


@api.get("/issues/{key}/comments/{comment_id}/reactions/", response=ReactionOut)
async def list_reactions(request, key: str, comment_id: int):
    comment = await _comment_in_issue(request, key, comment_id)
    rows = [r async for r in comment.reactions.select_related("user").order_by("created_at")]
    counts: dict[str, int] = {}
    for reaction in rows:
        counts[reaction.emoji] = counts.get(reaction.emoji, 0) + 1
    return ReactionOut(
        comment=comment.pk,
        counts=counts,
        mine=[row.emoji for row in rows if row.user_id == request.user.pk],
    )


class ReactionIn(Schema):
    #: One of jirrabit's six: +1, -1, heart, tada, eyes, rocket. Names, not the
    #: emoji characters, so an agent sending "+1" is not guessing at a glyph.
    emoji: str = Field(max_length=20)


@api.post("/issues/{key}/comments/{comment_id}/reactions/", response=ReactionOut)
async def add_reaction(request, key: str, comment_id: int, payload: ReactionIn):
    from ninja.errors import HttpError

    from issues.models import Reaction

    comment = await _comment_in_issue(request, key, comment_id)
    if payload.emoji not in dict(Reaction.EMOJIS):
        raise HttpError(400, f"emoji desconocido; usa uno de {', '.join(dict(Reaction.EMOJIS))}")
    await Reaction.objects.aget_or_create(comment=comment, user=request.user, emoji=payload.emoji)
    return await list_reactions(request, key, comment_id)


@api.delete("/issues/{key}/comments/{comment_id}/reactions/{emoji}/", response=ReactionOut)
async def remove_reaction(request, key: str, comment_id: int, emoji: str):
    from issues.models import Reaction

    comment = await _comment_in_issue(request, key, comment_id)
    removed, _ = await Reaction.objects.filter(comment=comment, user=request.user, emoji=emoji).adelete()
    if not removed:
        raise Http404
    return await list_reactions(request, key, comment_id)


# --- branch links ---


class BranchLinkOut(Schema):
    id: int
    issue: str
    branch: str
    repo_url: str = ""
    commit_sha: str = ""
    message: str = ""
    created_by: str = ""
    created_at: str


def _branch_out(b) -> BranchLinkOut:
    """Reads ``b.issue`` and ``b.created_by``, so the row must be select_related'd.

    An uncached relation read from an async view is a synchronous query, which is
    a 500 rather than a slow page.
    """
    return BranchLinkOut(
        id=b.pk,
        issue=b.issue.key,
        branch=b.branch,
        repo_url=b.repo_url or "",
        commit_sha=b.commit_sha or "",
        message=b.message or "",
        created_by=b.created_by.username if b.created_by_id else "",
        created_at=b.created_at.isoformat() if b.created_at else "",
    )


@api.get("/issues/{key}/branches/", response=Page[BranchLinkOut])
async def list_branches(request, key: str, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    """Branches and commits linked to an issue, newest first."""
    from issues.models import BranchLink

    issue = await _visible_issue(request, key)
    qs = BranchLink.objects.filter(issue=issue).select_related("issue", "created_by")
    return await paginate(qs, _branch_out, page, size)


class BranchLinkIn(Schema):
    branch: str = Field(max_length=200)
    repo_url: str = ""
    commit_sha: str = Field(default="", max_length=64)
    message: str = Field(default="", max_length=255)


@api.post("/issues/{key}/branches/", response=BranchLinkOut)
async def add_branch_link(request, key: str, payload: BranchLinkIn):
    """Link a branch or commit to an issue. Idempotent on issue+branch+sha."""
    from django.core.exceptions import ValidationError

    from issues.models import BranchLink

    issue = await _visible_issue(request, key)
    await _assert_can_edit(request, issue.project)
    if not payload.branch.strip():
        from ninja.errors import HttpError

        raise HttpError(400, "branch requerido")
    if payload.repo_url:
        # A URLField validates on save, so an unparseable one is a 500 unless it
        # is checked here.
        candidate = BranchLink(issue=issue, branch=payload.branch, repo_url=payload.repo_url)
        try:
            candidate.full_clean(exclude=["issue"])
        except ValidationError as exc:
            from ninja.errors import HttpError

            raise HttpError(400, f"repo_url no es una URL válida: {exc.messages}") from exc
    link, created = await BranchLink.objects.aget_or_create(
        issue=issue,
        branch=payload.branch.strip(),
        commit_sha=payload.commit_sha,
        defaults={
            "repo_url": payload.repo_url,
            "message": payload.message,
            "created_by": request.user,
        },
    )
    if not created and payload.message and link.message != payload.message:
        link.message = payload.message
        await link.asave(update_fields=["message"])
    # Re-read with the relations: only the created row carries the objects
    # aget_or_create was handed, and _branch_out reads two of them.
    link = await BranchLink.objects.filter(pk=link.pk).select_related("issue", "created_by").afirst()
    return _branch_out(link)


@api.delete("/issues/{key}/branches/{link_id}/", response=dict)
async def delete_branch_link(request, key: str, link_id: int):
    from issues.models import BranchLink

    issue = await _visible_issue(request, key)
    await _assert_can_edit(request, issue.project)
    removed, _ = await BranchLink.objects.filter(pk=link_id, issue=issue).adelete()
    if not removed:
        raise Http404
    return {"deleted": link_id}


# --- issue templates ---


class IssueTemplateOut(Schema):
    id: int
    name: str
    issue_type: str
    summary: str = ""
    description: str = ""
    priority: str = ""
    labels: list[str] = []
    created_by: str = ""
    created_at: str


async def _template_out(t) -> IssueTemplateOut:
    labels = [held.name async for held in t.labels.all()]
    labels.sort()
    return IssueTemplateOut(
        id=t.pk,
        name=t.name,
        issue_type=str(t.issue_type) if t.issue_type_id else "",
        summary=t.summary or "",
        description=t.description or "",
        priority=str(t.priority) if t.priority_id else "",
        labels=labels,
        created_by=t.created_by.username if t.created_by_id else "",
        created_at=t.created_at.isoformat() if t.created_at else "",
    )


@api.get("/projects/{key}/issue-templates/", response=Page[IssueTemplateOut])
async def list_issue_templates(request, key: str, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    """The reusable scaffolds for creating issues in this project."""
    project = await _visible_project(request, key)
    qs = project.issue_templates.select_related("issue_type", "priority", "created_by")
    return await paginate(qs, _template_out, page, size)


class IssueTemplateIn(Schema):
    name: str = Field(max_length=80)
    issue_type_id: int
    summary: str = Field(default="", max_length=255)
    description: str = ""
    priority_id: int | None = None
    labels: list[str] = []


@api.post("/projects/{key}/issue-templates/", response=IssueTemplateOut)
async def create_issue_template(request, key: str, payload: IssueTemplateIn):
    """Create a template. Needs admin: it is project configuration."""
    from ninja.errors import HttpError

    from issues.models import IssueTemplate

    project = await _visible_project(request, key)
    await _assert_project_admin(request, project)
    name = payload.name.strip()
    if not name:
        raise HttpError(400, "name requerido")
    if await IssueTemplate.objects.filter(project=project, name=name).aexists():
        raise HttpError(409, f"ya existe una plantilla llamada {name!r} en este proyecto")
    if not await IssueType.objects.filter(pk=payload.issue_type_id).aexists():
        raise HttpError(400, "tipo de tarea inválido")
    if payload.priority_id and not await Priority.objects.filter(pk=payload.priority_id).aexists():
        raise HttpError(400, "prioridad inválida")
    template = await IssueTemplate.objects.acreate(
        project=project,
        name=name,
        issue_type_id=payload.issue_type_id,
        summary=payload.summary,
        description=payload.description,
        priority_id=payload.priority_id,
        created_by=request.user,
    )
    if payload.labels:
        await template.labels.aset(await _validate_labels(payload.labels))
    # Re-read: acreate returns a row with no relations cached, and _template_out
    # reads three of them.
    template = (
        await IssueTemplate.objects.filter(pk=template.pk)
        .select_related("issue_type", "priority", "created_by")
        .afirst()
    )
    return await _template_out(template)


class IssueTemplatePatch(Schema):
    name: str | None = Field(default=None, max_length=80)
    issue_type_id: int | None = None
    summary: str | None = Field(default=None, max_length=255)
    description: str | None = None
    # Explicit null clears the priority: it is nullable, and there is no other
    # spelling for "no default priority".
    priority_id: int | None = None
    # The whole label set, replaced rather than merged: read the template first
    # or the labels it had are gone. Empty list clears them.
    labels: list[str] | None = None


@api.patch("/projects/{key}/issue-templates/{template_id}/", response=IssueTemplateOut)
async def patch_issue_template(request, key: str, template_id: int, payload: IssueTemplatePatch):
    """Edit a template. Needs admin: it is project configuration.

    The late arrival: create, list and delete existed while a typo in a
    template's default summary could only be fixed by deleting the template and
    recreating it. Same fields as the create, all optional, same validation.
    """
    from ninja.errors import HttpError

    from issues.models import IssueTemplate

    project = await _visible_project(request, key)
    await _assert_project_admin(request, project)
    template = await IssueTemplate.objects.filter(pk=template_id, project=project).afirst()
    if template is None:
        raise Http404
    data = payload.dict(exclude_unset=True)

    if "name" in data and data["name"] is not None:
        name = data["name"].strip()
        if not name:
            raise HttpError(400, "name requerido")
        if name != template.name and await IssueTemplate.objects.filter(project=project, name=name).aexists():
            raise HttpError(409, f"ya existe una plantilla llamada {name!r} en este proyecto")
        template.name = name

    if "issue_type_id" in data and data["issue_type_id"] is not None:
        if not await IssueType.objects.filter(pk=data["issue_type_id"]).aexists():
            raise HttpError(400, "tipo de tarea inválido")
        template.issue_type_id = data["issue_type_id"]

    if "summary" in data and data["summary"] is not None:
        template.summary = data["summary"]

    if "description" in data and data["description"] is not None:
        template.description = data["description"]

    if "priority_id" in data:
        if (
            data["priority_id"] is not None
            and not await Priority.objects.filter(pk=data["priority_id"]).aexists()
        ):
            raise HttpError(400, "prioridad inválida")
        template.priority_id = data["priority_id"]

    await template.asave()
    if "labels" in data and data["labels"] is not None:
        await template.labels.aset(await _validate_labels(data["labels"]))
    # Re-read: asave leaves the row's relations as they were, and _template_out
    # reads three of them.
    template = (
        await IssueTemplate.objects.filter(pk=template.pk)
        .select_related("issue_type", "priority", "created_by")
        .afirst()
    )
    return await _template_out(template)


@api.delete("/projects/{key}/issue-templates/{template_id}/", response=dict)
async def delete_issue_template(request, key: str, template_id: int):
    from issues.models import IssueTemplate

    project = await _visible_project(request, key)
    await _assert_project_admin(request, project)
    removed, _ = await IssueTemplate.objects.filter(pk=template_id, project=project).adelete()
    if not removed:
        raise Http404
    return {"deleted": template_id}


# --- CSV ------------------------------------------------------------------
#
# The web UI has had import and export since the beginning. Both are bulk
# operations over a project's whole issue set, which is exactly the shape an
# agent needs and cannot express over the per-issue endpoints, so they are here
# rather than waiting for someone to add them by hand.

#: The columns an export can contain, and the getter for each. Declared once
#: because the export view, the API and the tests all have to agree, and three
#: hand-maintained lists is how they stop agreeing.
CSV_COLUMNS = {
    "key": lambda i: i.key,
    "summary": lambda i: i.summary,
    "status": lambda i: str(i.status),
    "priority": lambda i: str(i.priority),
    "type": lambda i: str(i.issue_type),
    "assignee": lambda i: getattr(i.assignee, "username", "") or "",
    "reporter": lambda i: getattr(i.reporter, "username", "") or "",
    "sprint": lambda i: getattr(i.sprint, "name", "") or "",
    "epic": lambda i: getattr(i.epic, "name", "") or "",
    "story_points": lambda i: "" if i.story_points is None else i.story_points,
    "estimate_minutes": lambda i: "" if i.estimate_minutes is None else i.estimate_minutes,
    "time_spent_minutes": lambda i: i.time_spent_minutes or 0,
    "due_date": lambda i: i.due_date.isoformat() if i.due_date else "",
    "resolved_at": lambda i: i.resolved_at.isoformat() if i.resolved_at else "",
    "created_at": lambda i: i.created_at.isoformat(),
    "updated_at": lambda i: i.updated_at.isoformat(),
}

#: Columns an import understands. ``summary`` is the only required one; the rest
#: fall back to the first row of their table, which is what the web importer does
#: and is why a two-column CSV works.
CSV_IMPORT_COLUMNS = {
    "summary",
    "description",
    "type",
    "priority",
    "assignee",
    "story_points",
    "due_date",
}


class CsvExportOut(Schema):
    filename: str
    columns: list[str]
    #: The rows as data rather than as text, so a caller that wants to count or
    #: filter them does not have to parse a CSV out of a JSON envelope.
    rows: list[dict[str, str]]
    count: int


@api.get("/projects/{key}/csv-export/", response=CsvExportOut)
async def export_issues_csv(
    request,
    key: str,
    cols: str | None = None,
    text: str | None = None,
    status: int | None = None,
    assignee: str | None = None,
    archived: bool = False,
):
    """A project's issues as CSV rows.

    Honours the same filters the issue list does, so "export what I am looking at"
    is one call rather than a query built by hand.
    """
    import csv
    import io

    from django.utils import timezone

    project = await _visible_project(request, key)
    columns = [c for c in (cols or "").split(",") if c in CSV_COLUMNS] or list(CSV_COLUMNS)
    qs = project.issues.select_related(
        "status",
        "priority",
        "issue_type",
        "assignee",
        "reporter",
        "sprint",
        "epic",
    ).order_by("key")
    if text:
        from django.db.models import Q

        qs = qs.filter(Q(summary__icontains=text) | Q(key__icontains=text))
    if status is not None:
        qs = qs.filter(status_id=status)
    if assignee == "me":
        qs = qs.filter(assignee=request.user)
    elif assignee and assignee.isdigit():
        qs = qs.filter(assignee_id=int(assignee))
    if not archived:
        qs = qs.filter(archived=False)

    rows: list[dict[str, str]] = []
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    async for issue in qs:
        row = {column: str(CSV_COLUMNS[column](issue)) for column in columns}
        rows.append(row)
        writer.writerow([row[column] for column in columns])

    # Rows as data, not as a CSV string inside JSON. A caller that wants to count
    # or filter them should not have to write a parser, and the raw text is
    # available separately at /csv-export/download/ for a browser.
    return CsvExportOut(
        filename=f"{project.key}-issues-{timezone.now():%Y%m%d}.csv",
        columns=columns,
        rows=rows,
        count=len(rows),
    )


@api.get("/projects/{key}/csv-export/download/", response=dict)
async def download_issues_csv(request, key: str, cols: str | None = None, text: str | None = None):
    """The same export as a file download, for a browser rather than an agent.

    ``/csv-export/`` is the one an agent should use. This exists because an agent
    handing a person a file is a different request from a machine reading rows.
    """
    import csv
    import io

    from django.http import HttpResponse
    from django.utils import timezone

    project = await _visible_project(request, key)
    columns = [c for c in (cols or "").split(",") if c in CSV_COLUMNS] or list(CSV_COLUMNS)
    qs = project.issues.select_related(
        "status",
        "priority",
        "issue_type",
        "assignee",
        "reporter",
        "sprint",
        "epic",
    ).order_by("key")
    if text:
        from django.db.models import Q

        qs = qs.filter(Q(summary__icontains=text) | Q(key__icontains=text))
    qs = qs.filter(archived=False)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    async for issue in qs:
        writer.writerow([CSV_COLUMNS[c](issue) for c in columns])
    response = HttpResponse(buffer.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="{project.key}-issues-{timezone.now():%Y%m%d}.csv"'
    )
    return response


class CsvImportIn(Schema):
    csv: str
    #: True parses and reports; False creates. A bulk create cannot be undone, so
    #: the preview is the only chance to see what a malformed CSV would do.
    dry_run: bool = True
    #: Refuse the whole import if any row would fail, rather than creating the
    #: rows that happen to be valid. Off by default because the web importer does
    #: not offer it and a partial import is often what was wanted.
    strict: bool = False


class CsvImportRowOut(Schema):
    row: int
    summary: str
    issue_type: str = ""
    priority: str = ""
    assignee: str = ""
    story_points: str = ""
    due_date: str = ""
    #: Why this row was skipped, when it was. A row that is silently dropped is
    #: indistinguishable from one that was never sent.
    skipped: bool = False
    problem: str = ""


class CsvImportOut(Schema):
    dry_run: bool
    total_rows: int
    creatable: int
    rows: list[CsvImportRowOut]
    created_keys: list[str] = []
    #: Names that could not be resolved, so a typo in a type or priority is
    #: visible without opening the CSV again.
    unknown_values: dict[str, list[str]] = {}


def _parse_csv_import(text: str) -> list[dict]:
    """Normalise a CSV into lowercased keys, one dict per row, numbered from 1.

    Separate from both endpoints so preview and apply cannot disagree about what a
    CSV means, which is the failure mode of two implementations of one parser.
    A row without a summary is kept rather than dropped, so the preview can say
    which row number was rejected instead of the count just being lower.
    """
    import csv as _csv
    import io

    reader = _csv.DictReader(io.StringIO(text))
    parsed: list[dict] = []
    for raw in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        row["_row"] = len(parsed) + 1
        parsed.append(row)
    return parsed


@api.post("/projects/{key}/csv-import/", response=CsvImportOut)
async def import_issues_csv(request, key: str, payload: CsvImportIn):
    """Preview or apply a CSV import.

    ``dry_run`` defaults to true: it parses, resolves every value it can, and
    reports what would happen without creating anything. Only ``dry_run: false``
    creates issues, one ``asave()`` each so notifications, audit, webhooks and the
    realtime broadcast all fire — a ``bulk_create`` here would create rows that
    nobody was told about and that no board would show.
    """
    from issues.models import IssueType, Priority

    project = await _visible_project(request, key)
    await _assert_can_edit(request, project)
    text = (payload.csv or "").strip()
    if not text:
        from ninja.errors import HttpError

        raise HttpError(400, "csv vacío")

    parsed = _parse_csv_import(text)
    types_by_name = {t.name.lower(): t async for t in IssueType.objects.all()}
    priorities_by_name = {p.name.lower(): p async for p in Priority.objects.all()}
    usernames = {row["assignee"].lower() for row in parsed if row.get("assignee")}
    users_by_name = (
        {u.username.lower(): u async for u in User.objects.filter(username__in=usernames)}
        if usernames
        else {}
    )
    default_type = next(iter(types_by_name.values()), None)
    default_priority = next(iter(priorities_by_name.values()), None)
    default_status = await Status.objects.order_by("order").afirst()

    unknown: dict[str, set[str]] = {}
    reported: list[CsvImportRowOut] = []
    creatable: list[dict] = []
    for row in parsed:
        out = CsvImportRowOut(
            row=row["_row"],
            summary=row.get("summary", "")[:255],
            issue_type=row.get("type", ""),
            priority=row.get("priority", ""),
            assignee=row.get("assignee", ""),
            story_points=row.get("story_points", ""),
            due_date=row.get("due_date", ""),
        )
        if not out.summary:
            out.skipped = True
            out.problem = "falta summary"
            reported.append(out)
            continue
        for field, table in (("type", types_by_name), ("priority", priorities_by_name)):
            value = row.get(field, "").lower()
            if value and value not in table:
                unknown.setdefault(field, set()).add(row[field])
        # A value that resolves to nothing falls back to the default rather than
        # failing the row, which is what the web importer does. The typo shows up
        # in `unknown_values` instead, which is where it is actually useful.
        itype = types_by_name.get(row.get("type", "").lower(), default_type)
        prio = priorities_by_name.get(row.get("priority", "").lower(), default_priority)
        assignee = users_by_name.get(row.get("assignee", "").lower())
        if default_type is None or default_status is None:
            out.skipped = True
            out.problem = "la instancia no tiene tipos de tarea o estados; ejecuta seed_jirrabit"
            reported.append(out)
            continue
        creatable.append(
            {
                "summary": out.summary,
                "description": row.get("description", ""),
                "issue_type": itype,
                "priority": prio,
                "assignee": assignee,
                "story_points": row.get("story_points") or None,
                "due_date": row.get("due_date") or None,
            }
        )
        reported.append(out)

    if payload.dry_run:
        return CsvImportOut(
            dry_run=True,
            total_rows=len(parsed),
            creatable=len(creatable),
            rows=reported,
            unknown_values={k: sorted(v) for k, v in unknown.items()},
        )

    created: list[str] = []
    for item in creatable:
        issue = Issue(
            project=project,
            reporter=request.user,
            summary=item["summary"],
            description=item["description"],
            status=default_status,
            priority=item["priority"],
            issue_type=item["issue_type"],
            assignee=item["assignee"],
        )
        if item["story_points"]:
            try:
                issue.story_points = int(item["story_points"])
            except ValueError:
                pass
        if item["due_date"]:
            from datetime import date as _date

            try:
                issue.due_date = _date.fromisoformat(item["due_date"])
            except ValueError:
                pass
        # asave, not acreate-in-a-loop-batched: each insert runs the four
        # post_save receivers, so every created issue notifies its watchers, leaves
        # an audit row, fires webhooks and pushes over the websocket.
        await issue.asave()
        created.append(issue.key)
    return CsvImportOut(
        dry_run=False,
        total_rows=len(parsed),
        creatable=len(creatable),
        rows=reported,
        created_keys=created,
        unknown_values={k: sorted(v) for k, v in unknown.items()},
    )


# --- saved board views -----------------------------------------------------
#
# A saved combination of board filters. The board GET params are the source of
# truth, so a view is a URL rather than a query, and applying one needs no
# server-side resolution.

BOARD_VIEW_FILTERS = {"assignee", "type", "priority", "epic", "sprint", "stale", "due", "text"}


class BoardViewOut(Schema):
    id: int
    name: str
    filters: dict[str, str] = {}
    is_default: bool = False
    #: The board URL with the filters applied, so a client that renders a link
    #: does not have to know the parameter names.
    url: str = ""
    created_at: str


class BoardViewIn(Schema):
    name: str = Field(max_length=80)
    filters: dict[str, str] = {}
    is_default: bool = False


def _board_view_out(view) -> BoardViewOut:
    from urllib.parse import urlencode

    query = urlencode({k: v for k, v in (view.filters or {}).items() if v})
    return BoardViewOut(
        id=view.pk,
        name=view.name,
        filters=view.filters or {},
        is_default=view.is_default,
        url=f"/board/{view.project.key}/?{query}" if query else f"/board/{view.project.key}/",
        created_at=view.created_at.isoformat() if view.created_at else "",
    )


@api.get("/projects/{key}/board-views/", response=Page[BoardViewOut])
async def list_board_views(request, key: str, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    """The caller's saved board views for this project."""
    project = await _visible_project(request, key)
    from board.models import SavedBoardView

    views = (
        SavedBoardView.objects.filter(user=request.user, project=project)
        .select_related("project")
        .order_by("name")
    )
    return await paginate(views, _board_view_out, page, size)


@api.post("/projects/{key}/board-views/", response=BoardViewOut)
async def create_board_view(request, key: str, payload: BoardViewIn):
    """Save the current board filters under a name.

    Only the caller's own views, and only the filter names the board itself
    reads: an unrecognised key would be stored, never applied, and read back as
    though it worked.
    """
    from ninja.errors import HttpError

    from board.models import SavedBoardView

    project = await _visible_project(request, key)
    name = payload.name.strip()
    if not name:
        raise HttpError(400, "name requerido")
    unknown = set(payload.filters) - BOARD_VIEW_FILTERS
    if unknown:
        raise HttpError(
            400,
            f"filtros no reconocidos: {', '.join(sorted(unknown))}; "
            f"los válidos son {', '.join(sorted(BOARD_VIEW_FILTERS))}",
        )
    view, created = await SavedBoardView.objects.aget_or_create(
        user=request.user,
        project=project,
        name=name,
        defaults={"filters": payload.filters, "is_default": payload.is_default},
    )
    if not created:
        # A PUT would be a lie about the rest of the row, and this is the only
        # thing the caller can be asking for: "save these filters under this name".
        view.filters = payload.filters
        view.is_default = payload.is_default
        await view.asave(update_fields=["filters", "is_default"])
    if payload.is_default:
        # One default per project per person: a second default would leave which
        # one applies up to row order.
        await (
            SavedBoardView.objects.filter(
                user=request.user,
                project=project,
                is_default=True,
            )
            .exclude(pk=view.pk)
            .aupdate(is_default=False)
        )
    # Re-read with the relation: aget_or_create carries `project` on the row it
    # created and not on the one it found, and _board_view_out reads project.key.
    view = await SavedBoardView.objects.filter(pk=view.pk).select_related("project").afirst()
    return _board_view_out(view)


@api.delete("/projects/{key}/board-views/{view_id}/", response=dict)
async def delete_board_view(request, key: str, view_id: int):
    from board.models import SavedBoardView

    project = await _visible_project(request, key)
    removed, _ = await SavedBoardView.objects.filter(pk=view_id, user=request.user, project=project).adelete()
    if not removed:
        raise Http404
    return {"deleted": view_id}


# --- recently viewed -------------------------------------------------------
#
# One row per (person, issue), bumped on every open. The dashboard's "Recientes"
# is a list of keys, and this is the same list.


class RecentVisitOut(Schema):
    issue: str
    summary: str
    status: str
    viewed_at: str
    #: True when the issue was archived after the last visit, which is why it can
    #: be absent from the board the person is looking at.
    archived: bool = False


@api.get("/recent/", response=Page[RecentVisitOut])
async def list_recent_issues(request, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    """The caller's most recently opened issues, newest first.

    Filtered through ``filter_visible``, so a project the caller has since lost
    access to drops out of the list rather than leaving a key that 404s on click.
    """
    from issues.models import Visit

    visible = Project.objects.filter_visible(request.user).values_list("pk", flat=True)
    visits = (
        Visit.objects.filter(user=request.user, issue__project__in=visible)
        .select_related("issue", "issue__status")
        .order_by("-viewed_at")
    )
    return await paginate(visits, _recent_out, page, size)


async def _recent_out(visit) -> RecentVisitOut:
    return RecentVisitOut(
        issue=visit.issue.key,
        summary=visit.issue.summary,
        status=str(visit.issue.status),
        viewed_at=visit.viewed_at.isoformat() if visit.viewed_at else "",
        archived=visit.issue.archived,
    )


@api.delete("/recent/", response=dict)
async def clear_recent_issues(request):
    """Forget every visit. The issues themselves are untouched."""
    from issues.models import Visit

    removed, _ = await Visit.objects.filter(user=request.user).adelete()
    return {"cleared": removed}


@api.delete("/recent/{issue_key}/", response=dict)
async def forget_recent_issue(request, issue_key: str):
    from issues.models import Visit

    issue = await _visible_issue(request, issue_key)
    removed, _ = await Visit.objects.filter(user=request.user, issue=issue).adelete()
    if not removed:
        raise Http404
    return {"forgotten": issue.key}


# --- mention receipts ------------------------------------------------------
#
# Whether an @mention was actually read. Created by the notification pass, marked
# seen when the mentioned person opens the issue. An agent asking "did anyone see
# what I asked" is asking a real question that had no answer.


class MentionOut(Schema):
    comment: int
    actor: str
    mentioned: str
    created_at: str
    seen_at: str = ""
    seen: bool = False


async def _mention_out(receipt) -> MentionOut:
    return MentionOut(
        comment=receipt.comment_id,
        actor=receipt.actor.username,
        mentioned=receipt.mentioned.username,
        created_at=receipt.created_at.isoformat() if receipt.created_at else "",
        seen_at=receipt.seen_at.isoformat() if receipt.seen_at else "",
        seen=receipt.seen_at is not None,
    )


@api.get("/issues/{key}/comments/{comment_id}/mentions/", response=dict)
async def list_comment_mentions(request, key: str, comment_id: int):
    """Who was @mentioned in a comment, and whether they have opened the issue.

    Visible to anyone who can see the issue, not just the comment's author: the
    mention list is already public in the comment body, and hiding who was named
    would be theatre. The *timestamps* are the author's business, so they are
    only included for the author and the mentioned people.
    """
    from accounts.models import MentionReceipt

    issue = await _visible_issue(request, key)
    comment = await Comment.objects.filter(pk=comment_id, issue=issue).select_related("author").afirst()
    if comment is None:
        raise Http404
    receipts = [
        r
        async for r in MentionReceipt.objects.filter(comment=comment)
        .select_related("actor", "mentioned")
        .order_by("created_at")
    ]
    # A caller who may see who was named but is neither them nor the author does
    # not get the read times. Naming somebody is public; reading is not.
    involved = request.user.pk in {comment.author_id} | {r.mentioned_id for r in receipts}
    items = []
    for receipt in receipts:
        out = await _mention_out(receipt)
        if not involved:
            out.seen_at = ""
            out.seen = False
        items.append(out)
    return {
        "issueIdOrKey": issue.key,
        "commentId": comment.pk,
        "mentions": items,
        "count": len(items),
        "seenCount": sum(1 for r in receipts if r.seen_at is not None),
    }


# --- invites ---------------------------------------------------------------
#
# Invite-only registration has a UI and no API, so a scripted onboarding run
# could not use it. Superuser-only, like the page behind it.


class InviteOut(Schema):
    id: int
    #: Returned only on creation. The token is a bearer credential for a
    #: registration, so it is not readable afterwards from any list.
    token: str = ""
    registration_url: str = ""
    email: str = ""
    role: str = "member"
    created_at: str = ""
    expires_at: str = ""
    used_at: str = ""
    used_by: str = ""
    valid: bool = False


def _invite_out(invite, token: str = "", url: str = "") -> InviteOut:
    return InviteOut(
        id=invite.pk,
        token=token,
        registration_url=url,
        email=invite.email or "",
        role=invite.role,
        created_at=invite.created_at.isoformat() if invite.created_at else "",
        expires_at=invite.expires_at.isoformat() if invite.expires_at else "",
        used_at=invite.used_at.isoformat() if invite.used_at else "",
        used_by=invite.used_by.username if invite.used_by_id else "",
        valid=invite.is_valid,
    )


@api.get("/admin/invites/", response=Page[InviteOut])
async def list_invites(request, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    """Every registration invite, for a superuser.

    Tokens are not included: an invite is a credential, and a list that handed
    them out would be a page of bearer tokens for anyone who could open it. The
    token comes back from the call that creates one, which is the only moment the
    caller needs it.
    """
    from ninja.errors import HttpError

    from accounts.models import InviteToken

    if not request.user.is_superuser:
        raise HttpError(403, "Requiere superusuario")
    invites = InviteToken.objects.all().select_related("created_by", "used_by")
    return await paginate(invites, _invite_out, page, size)


class InviteIn(Schema):
    email: str = ""
    role: str = "member"
    days: int = 7


@api.post("/admin/invites/", response=InviteOut)
async def create_invite(request, payload: InviteIn):
    """Mint a registration invite. Superuser only.

    The only response that carries the token, and the only way to obtain the
    registration URL — which is why revoking is not the way to rotate one: mint a
    new invite instead.
    """
    import secrets
    from datetime import timedelta

    from django.utils import timezone
    from ninja.errors import HttpError

    from accounts.models import InviteToken

    if not request.user.is_superuser:
        raise HttpError(403, "Requiere superusuario")
    if payload.role not in {"member", "admin", "viewer"}:
        raise HttpError(400, "role debe ser member, admin o viewer")
    days = max(1, min(payload.days, 365))
    invite = await InviteToken.objects.acreate(
        created_by=request.user,
        email=payload.email.strip(),
        role=payload.role,
        token=secrets.token_urlsafe(32),
        expires_at=timezone.now() + timedelta(days=days),
    )
    url = request.build_absolute_uri(f"/accounts/register/?token={invite.token}")
    return _invite_out(invite, token=invite.token, url=url)


@api.delete("/admin/invites/{invite_id}/", response=dict)
async def revoke_invite(request, invite_id: int):
    """Revoke an unused invite by expiring it now.

    An expiry rather than a delete, so a registration that is already in flight
    fails cleanly with "expired" instead of "no such invite", and so the row
    remains as a record that the invite existed.
    """
    from django.utils import timezone
    from ninja.errors import HttpError

    from accounts.models import InviteToken

    if not request.user.is_superuser:
        raise HttpError(403, "Requiere superusuario")
    invite = await InviteToken.objects.filter(pk=invite_id).afirst()
    if invite is None:
        raise Http404
    if invite.used_at is None:
        invite.expires_at = timezone.now()
        await invite.asave(update_fields=["expires_at"])
    return {"revoked": invite_id, "token": "", "valid": invite.is_valid}


# --- workflow vocabulary ---
#
# Status, Priority, IssueType and Label were readable and writable only through a
# superuser-only web editor. These make each of them CRUD, with the two properties
# the editor is careful about and a naive implementation gets wrong:
#
#   - They are ``on_delete=PROTECT``, so deleting one that is in use raises rather
#     than orphaning every issue that had it. The editor catches that and shows a
#     message; here it is a 409 naming the count. Silently detaching the value from
#     thousands of issues is the failure this refuses to allow.
#   - ``Status.allowed_next`` is what makes a workflow restricted, and an empty
#     list means *open*, not *closed*. A status that loses its allowed_next does
#     not become stricter, it becomes permissive — so the transition endpoints take
#     the list explicitly and never touch it unless asked.
#
# All superuser-only, matching the editor.


async def _require_superuser(request) -> None:
    from ninja.errors import HttpError

    if not request.user.is_superuser:
        raise HttpError(403, "Requiere superusuario")


async def _used_by_issues(field: str, pk: int) -> int:
    """How many issues carry this status/priority/type, for the 409 message.

    Named by the model's field name rather than by the class, so one helper covers
    all three and there is no place for the mapping to drift.
    """
    return await Issue.objects.filter(**{f"{field}_id": pk}).acount()


class StatusIn(Schema):
    #: max_length mirrors the model column. Without it an over-long name reaches
    #: Postgres and answers 500, which is the least useful thing to say about a typo.
    name: str = Field(max_length=40)
    category: str = Field(default="todo", max_length=16)
    #: No default on purpose. ``Status.Meta.ordering`` is ("order", "id"), so a
    #: default of 0 would put every new status at the FRONT of the board and
    #: silently reorder the columns of a live project. Omit it and the status goes
    #: on the end, which is what someone adding a column almost always means.
    order: int | None = None
    wip_limit: int | None = None


class StatusPatch(Schema):
    name: str | None = Field(default=None, max_length=40)
    category: str | None = Field(default=None, max_length=16)
    order: int | None = None
    wip_limit: int | None = None
    #: Ids of the statuses reachable from this one. Omit to leave the workflow
    #: untouched; send an empty list to make it open, which is what the model means
    #: by having no rules at all.
    allowed_next: list[int] | None = None


@api.post("/statuses/", response=StatusOut)
async def create_status(request, payload: StatusIn):
    from ninja.errors import HttpError

    await _require_superuser(request)
    name = payload.name.strip()
    if not name:
        raise HttpError(400, "name requerido")
    if payload.category not in dict(Status.CATEGORY):
        raise HttpError(400, f"category inválida; usa una de {', '.join(dict(Status.CATEGORY))}")
    if await Status.objects.filter(name=name).aexists():
        raise HttpError(409, f"ya existe un estado llamado {name!r}")
    order = payload.order
    if order is None:
        # After everything that exists, rather than at 0 and therefore first.
        last = await Status.objects.order_by("-order").afirst()
        order = (last.order + 10) if last else 10
    return await Status.objects.acreate(
        name=name, category=payload.category, order=order, wip_limit=payload.wip_limit
    )


@api.patch("/statuses/{status_id}/", response=StatusOut)
async def patch_status(request, status_id: int, payload: StatusPatch):
    from ninja.errors import HttpError

    await _require_superuser(request)
    status = await Status.objects.filter(pk=status_id).afirst()
    if status is None:
        raise Http404
    data = payload.dict(exclude_unset=True)
    if "category" in data and data["category"] not in dict(Status.CATEGORY):
        raise HttpError(400, f"category inválida; usa una de {', '.join(dict(Status.CATEGORY))}")
    allowed = data.pop("allowed_next", None)
    for field, value in data.items():
        setattr(status, field, value.strip() if field == "name" and value else value)
    await status.asave()
    if allowed is not None:
        targets = [s async for s in Status.objects.filter(pk__in=allowed)]
        found = {s.pk for s in targets}
        if len(found) != len(set(allowed)):
            raise HttpError(400, f"estados inexistentes: {sorted(set(allowed) - found)}")
        # aset, not add: the caller sent the whole list, and "add these" would
        # leave no way to remove a transition.
        await status.allowed_next.aset(targets)
    return status


@api.delete("/statuses/{status_id}/", response=dict)
async def delete_status(request, status_id: int):
    from ninja.errors import HttpError

    await _require_superuser(request)
    status = await Status.objects.filter(pk=status_id).afirst()
    if status is None:
        raise Http404
    in_use = await _used_by_issues("status", status_id)
    if in_use:
        raise HttpError(409, f"el estado está en uso por {in_use} tareas")
    await status.adelete()
    return {"deleted": status_id}


class PriorityIn(Schema):
    #: 20, not the 40 Status.name allows: Priority.name is a shorter column and a
    #: 25-character name is a perfectly reasonable thing to try.
    name: str = Field(max_length=20)
    weight: int = 0
    color: str = Field(default="#1e6fff", max_length=20)


class PriorityPatch(Schema):
    name: str | None = Field(default=None, max_length=20)
    weight: int | None = None
    color: str | None = Field(default=None, max_length=20)


@api.post("/priorities/", response=PriorityOut)
async def create_priority(request, payload: PriorityIn):
    from ninja.errors import HttpError

    await _require_superuser(request)
    name = payload.name.strip()
    if not name:
        raise HttpError(400, "name requerido")
    if await Priority.objects.filter(name=name).aexists():
        raise HttpError(409, f"ya existe una prioridad llamada {name!r}")
    return await Priority.objects.acreate(name=name, weight=payload.weight, color=payload.color)


@api.patch("/priorities/{priority_id}/", response=PriorityOut)
async def patch_priority(request, priority_id: int, payload: PriorityPatch):
    await _require_superuser(request)
    row = await Priority.objects.filter(pk=priority_id).afirst()
    if row is None:
        raise Http404
    for field, value in payload.dict(exclude_unset=True).items():
        setattr(row, field, value.strip() if field == "name" and value else value)
    await row.asave()
    return row


@api.delete("/priorities/{priority_id}/", response=dict)
async def delete_priority(request, priority_id: int):
    from ninja.errors import HttpError

    await _require_superuser(request)
    row = await Priority.objects.filter(pk=priority_id).afirst()
    if row is None:
        raise Http404
    in_use = await _used_by_issues("priority", priority_id)
    if in_use:
        raise HttpError(409, f"la prioridad está en uso por {in_use} tareas")
    await row.adelete()
    return {"deleted": priority_id}


class IssueTypeIn(Schema):
    name: str = Field(max_length=40)
    category: str = Field(default="task", max_length=16)
    icon: str = Field(default="", max_length=8)
    color: str = Field(default="#1e6fff", max_length=20)


class IssueTypePatch(Schema):
    name: str | None = Field(default=None, max_length=40)
    category: str | None = Field(default=None, max_length=16)
    icon: str | None = Field(default=None, max_length=8)
    color: str | None = Field(default=None, max_length=20)


@api.post("/issue-types/", response=IssueTypeOut)
async def create_issue_type(request, payload: IssueTypeIn):
    from ninja.errors import HttpError

    await _require_superuser(request)
    name = payload.name.strip()
    if not name:
        raise HttpError(400, "name requerido")
    if await IssueType.objects.filter(name=name).aexists():
        raise HttpError(409, f"ya existe un tipo llamado {name!r}")
    return await IssueType.objects.acreate(
        name=name, category=payload.category, icon=payload.icon, color=payload.color
    )


@api.patch("/issue-types/{type_id}/", response=IssueTypeOut)
async def patch_issue_type(request, type_id: int, payload: IssueTypePatch):
    await _require_superuser(request)
    row = await IssueType.objects.filter(pk=type_id).afirst()
    if row is None:
        raise Http404
    for field, value in payload.dict(exclude_unset=True).items():
        setattr(row, field, value.strip() if field == "name" and value else value)
    await row.asave()
    return row


@api.delete("/issue-types/{type_id}/", response=dict)
async def delete_issue_type(request, type_id: int):
    from ninja.errors import HttpError

    await _require_superuser(request)
    row = await IssueType.objects.filter(pk=type_id).afirst()
    if row is None:
        raise Http404
    in_use = await _used_by_issues("issue_type", type_id)
    if in_use:
        raise HttpError(409, f"el tipo está en uso por {in_use} tareas")
    await row.adelete()
    return {"deleted": type_id}


class LabelPatch(Schema):
    name: str | None = Field(default=None, max_length=40)
    color: str | None = Field(default=None, max_length=20)


@api.patch("/labels/{label_id}/", response=LabelOut)
async def patch_label(request, label_id: int, payload: LabelPatch):
    from ninja.errors import HttpError

    await _require_superuser(request)
    row = await Label.objects.filter(pk=label_id).afirst()
    if row is None:
        raise Http404
    name = payload.name.strip() if payload.name else None
    if name and name != row.name and await Label.objects.filter(name=name).aexists():
        raise HttpError(409, f"ya existe una etiqueta llamada {name!r}")
    if name:
        row.name = name
    if payload.color is not None:
        row.color = payload.color
    await row.asave()
    return row


# --- custom fields ---


class CustomFieldDefOut(Schema):
    id: int
    name: str
    slug: str
    type: str
    required: bool = False
    #: A list, where the model stores one comma-separated string.
    options: list[str] = []
    order: int = 0

    @staticmethod
    def from_def(d) -> CustomFieldDefOut:
        return CustomFieldDefOut(
            id=d.pk,
            name=d.name,
            slug=d.slug,
            type=d.type,
            required=d.required,
            options=d.option_list,
            order=d.order,
        )


class CustomFieldDefIn(Schema):
    name: str = Field(max_length=80)
    #: Derived from the name when omitted. Unique per project, which is what makes
    #: it usable as the key in Issue.custom_fields.
    slug: str | None = Field(default=None, max_length=80)
    type: str = Field(default="text", max_length=16)
    required: bool = False
    options: str = ""
    order: int = 0


@api.get("/projects/{key}/custom-fields/", response=Page[CustomFieldDefOut])
async def list_custom_fields(request, key: str, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    """A project's custom field definitions, in display order."""
    project = await _visible_project(request, key)
    qs = project.custom_fields.order_by("order", "id")
    return await paginate(qs, CustomFieldDefOut.from_def, page, size)


@api.post("/projects/{key}/custom-fields/", response=CustomFieldDefOut)
async def create_custom_field(request, key: str, payload: CustomFieldDefIn):
    from ninja.errors import HttpError

    from projects.models import CustomFieldDef

    project = await _visible_project(request, key)
    await _assert_project_admin(request, project)
    name = payload.name.strip()
    if not name:
        raise HttpError(400, "name requerido")
    valid = dict(CustomFieldDef.TYPE_CHOICES)
    if payload.type not in valid:
        raise HttpError(400, f"type inválido; usa uno de {', '.join(valid)}")
    slug = (payload.slug or name).strip().lower().replace(" ", "-")
    if await CustomFieldDef.objects.filter(project=project, slug=slug).aexists():
        raise HttpError(409, f"ya existe un campo con el slug {slug!r} en este proyecto")
    row = await CustomFieldDef.objects.acreate(
        project=project,
        name=name,
        slug=slug,
        type=payload.type,
        required=payload.required,
        options=payload.options,
        order=payload.order,
    )
    return CustomFieldDefOut.from_def(row)


@api.delete("/projects/{key}/custom-fields/{field_id}/", response=dict)
async def delete_custom_field(request, key: str, field_id: int):
    from projects.models import CustomFieldDef

    project = await _visible_project(request, key)
    await _assert_project_admin(request, project)
    row = await CustomFieldDef.objects.filter(pk=field_id, project=project).afirst()
    if row is None:
        raise Http404
    # The values live in Issue.custom_fields keyed by slug, so deleting the
    # definition leaves them behind as keys nothing will read again. Reported
    # rather than cleaned, because the data is the user's and the count is what
    # lets them decide.
    holders = await Issue.objects.filter(project=project, custom_fields__has_key=row.slug).acount()
    await row.adelete()
    return {"deleted": field_id, "issues_that_still_carry_its_values": holders}


class CustomFieldValues(Schema):
    #: Keyed by the definition's slug, which is the same key the values are stored
    #: under. A bare object rather than a list of {slug, value} pairs because that
    #: is the shape on the way in and on the way out.
    customFields: dict[str, object] = {}


@api.get("/issues/{key}/custom-fields/", response=CustomFieldValues)
async def get_issue_custom_fields(request, key: str):
    """An issue's custom field values, keyed by slug."""
    issue = await _visible_issue(request, key)
    return CustomFieldValues(customFields=issue.custom_fields or {})


@api.patch("/issues/{key}/custom-fields/", response=CustomFieldValues)
async def patch_issue_custom_fields(request, key: str, payload: CustomFieldValues):
    """Set custom field values on an issue.

    An unknown slug is refused rather than written: a typo would otherwise leave a
    value that no definition describes and nothing would ever read it. A null
    removes the key, so "unset" does not linger as a stored null.
    """
    from ninja.errors import HttpError

    from projects.models import CustomFieldDef

    issue = await _visible_issue(request, key)
    await _assert_can_edit(request, issue.project)
    known = {d.slug async for d in CustomFieldDef.objects.filter(project=issue.project)}
    unknown = sorted(set(payload.customFields) - known)
    if unknown:
        raise HttpError(
            400,
            f"campos desconocidos para este proyecto: {unknown}. Definidos: {sorted(known) or 'ninguno'}",
        )
    values = dict(issue.custom_fields or {})
    for slug, value in payload.customFields.items():
        if value is None:
            values.pop(slug, None)
        else:
            values[slug] = value
    issue.custom_fields = values
    await issue.asave(update_fields=["custom_fields", "updated_at"])
    return CustomFieldValues(customFields=values)


# --- webhooks ---
#
# A webhook is configuration with no effect: jirrabit dispatches to in-process
# stub actions that mostly log, and there is no outbound HTTP. Creating one over
# the API is still worth having — the definition is real and survives, and seeing
# the registry an action code comes from is the part an operator needs.


class WebhookOut(Schema):
    id: int
    name: str
    #: Empty for an instance-wide hook, which is not the same as one scoped to the
    #: project being listed: both appear here, and a caller has to tell them apart.
    project: str = ""
    action: str = ""
    entity: str = "issue"
    event: str = "issue.updated"
    state_filter: list[str] = []
    active: bool = True
    created_at: str
    last_status: int | None = None
    last_error: str = ""
    last_delivered_at: str = ""


def _webhook_out(w) -> WebhookOut:
    return WebhookOut(
        id=w.pk,
        name=w.name,
        project=w.project.key if w.project_id else "",
        action=w.action or "",
        entity=w.entity,
        event=w.event,
        state_filter=[s.strip() for s in (w.state_filter or "").split(",") if s.strip()],
        active=w.active,
        created_at=w.created_at.isoformat() if w.created_at else "",
        last_status=w.last_status,
        last_error=w.last_error or "",
        last_delivered_at=w.last_delivered_at.isoformat() if w.last_delivered_at else "",
    )


class WebhookIn(Schema):
    name: str = Field(max_length=80)
    #: The scope is the project in the path, not here: a hook created under
    #: /projects/WEB/ is that project's, whatever this says.
    action: str = Field(default="", max_length=120)
    entity: str = Field(default="issue", max_length=32)
    event: str = Field(default="issue.updated", max_length=64)
    #: Comma-separated state names; empty fires on any state.
    state_filter: str = Field(default="", max_length=255)
    active: bool = True


@api.get("/projects/{key}/webhooks/", response=Page[WebhookOut])
async def list_webhooks(request, key: str, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    """Webhooks scoped to a project, plus the instance-wide ones."""
    from projects.models import Webhook

    project = await _visible_project(request, key)
    qs = (
        Webhook.objects.filter(models.Q(project=project) | models.Q(project__isnull=True))
        .select_related("project")
        .order_by("name")
    )
    return await paginate(qs, _webhook_out, page, size)


@api.post("/projects/{key}/webhooks/", response=WebhookOut)
async def create_webhook(request, key: str, payload: WebhookIn):
    from ninja.errors import HttpError

    from projects.models import Webhook

    project = await _visible_project(request, key)
    await _assert_project_admin(request, project)
    name = payload.name.strip()
    if not name:
        raise HttpError(400, "name requerido")
    row = await Webhook.objects.acreate(
        project=project,
        name=name,
        action=payload.action,
        entity=payload.entity,
        event=payload.event,
        state_filter=payload.state_filter,
        active=payload.active,
    )
    return _webhook_out(row)


@api.patch("/projects/{key}/webhooks/{hook_id}/", response=WebhookOut)
async def patch_webhook(request, key: str, hook_id: int, payload: WebhookIn):
    from projects.models import Webhook

    project = await _visible_project(request, key)
    await _assert_project_admin(request, project)
    row = await Webhook.objects.filter(pk=hook_id, project=project).select_related("project").afirst()
    if row is None:
        # A hook belonging to another project is a 404, not a 403: the caller
        # cannot see it, and the id is not theirs to be told about.
        raise Http404
    for field, value in payload.dict(exclude_unset=True).items():
        setattr(row, field, value)
    await row.asave()
    return _webhook_out(row)


@api.delete("/projects/{key}/webhooks/{hook_id}/", response=dict)
async def delete_webhook(request, key: str, hook_id: int):
    from projects.models import Webhook

    project = await _visible_project(request, key)
    await _assert_project_admin(request, project)
    removed, _ = await Webhook.objects.filter(pk=hook_id, project=project).adelete()
    if not removed:
        raise Http404
    return {"deleted": hook_id}


# --- project wiki ---


class WikiOut(Schema):
    project: str
    #: Markdown, and the whole page. The model holds one blob, not sections.
    body: str = ""
    updated_at: str = ""
    updated_by: str = ""


@api.get("/projects/{key}/wiki/", response=WikiOut)
async def get_project_wiki(request, key: str):
    """A project's wiki page. Empty rather than 404 when it has never been written.

    One page per project, which is what the model holds, so an unwritten page is a
    normal state for most projects rather than a missing resource.
    """
    from projects.models import ProjectWiki

    project = await _visible_project(request, key)
    page = await ProjectWiki.objects.filter(project=project).select_related("updated_by").afirst()
    if page is None:
        return WikiOut(project=key)
    return WikiOut(
        project=key,
        body=page.body or "",
        updated_at=page.updated_at.isoformat() if page.updated_at else "",
        updated_by=page.updated_by.username if page.updated_by_id else "",
    )


class WikiIn(Schema):
    #: The body only. The response schema is not reused as the request body,
    #: because it carries `project` and `updated_by` — fields the caller has no
    #: reason to send, and answering 422 for a missing one is a bad first
    #: impression of a one-field document.
    body: str = ""


@api.put("/projects/{key}/wiki/", response=WikiOut)
async def put_project_wiki(request, key: str, payload: WikiIn):
    """Write a project's wiki page, replacing what was there.

    A PUT rather than a PATCH because the page is one document: two writers
    replacing it wholesale is the honest model, and a partial update on a single
    blob invites a lost edit with nothing to merge.
    """
    from projects.models import ProjectWiki

    project = await _visible_project(request, key)
    await _assert_project_admin(request, project)
    page, created = await ProjectWiki.objects.aget_or_create(project=project, defaults={"body": payload.body})
    page.body = payload.body
    page.updated_by = request.user
    await page.asave(update_fields=["body", "updated_by", "updated_at"])
    return WikiOut(
        project=key,
        body=page.body or "",
        updated_at=page.updated_at.isoformat() if page.updated_at else "",
        updated_by=page.updated_by.username if page.updated_by_id else "",
    )


# --- labels ---

#
# Labels were readable and unwritable: IssueOut listed them, JQL filtered on them,
# and neither IssueIn nor IssuePatch had a field for them. A client could find
# every issue tagged `urgent` and could not tag anything.
#
# A label is a bare word, not a record with an id anybody remembers, so the write
# side takes names and creates what is missing. The CRUD endpoints exist for the
# cases where that is not wanted: listing them to build a picker, and deleting
# one, which is superuser-only and PROTECT-aware exactly as the web editor is.


class LabelIn(Schema):
    name: str = Field(max_length=40)
    color: str = Field(default="#1e6fff", max_length=20)


# --- reading the past ---
#
# An agent can change an issue and cannot answer "what changed, who changed it,
# or when" — which is the question behind most reporting and most incident triage.
#
# Two different logs exist and they are not interchangeable. ``HistoryEntry`` is
# per-issue field history, written by two code paths and therefore incomplete;
# ``AuditEntry`` is a signal-driven project-wide activity feed, which is complete
# for creates, updates and deletes but carries no field-level before/after. Each
# endpoint says so in its docstring, because presenting either as "the history"
# would be the more convenient lie.


class ChangelogOut(Schema):
    id: int
    field: str
    old_value: str = ""
    new_value: str = ""
    actor: str = ""
    created_at: str

    @staticmethod
    def from_entry(h) -> ChangelogOut:
        return ChangelogOut(
            id=h.pk,
            field=h.field,
            old_value=h.old_value or "",
            new_value=h.new_value or "",
            actor=h.actor.username if h.actor_id else "",
            created_at=h.created_at.isoformat() if h.created_at else "",
        )


@api.get("/issues/{key}/changelog/", response=Page[ChangelogOut])
async def issue_changelog(
    request, key: str, page: int = 1, size: int = DEFAULT_PAGE_SIZE, field: str | None = None
):
    """Field-level history for one issue, newest first.

    Incomplete by construction, and the endpoint says so rather than letting a
    caller read silence as "nothing changed": ``HistoryEntry`` is written by only
    two code paths, so a field edited anywhere else leaves no row. A changelog
    that is complete for some fields and silent for others is the dangerous kind
    — it looks authoritative and is not. ``field`` narrows to one name, which is
    also the honest way to ask "when did the status last change".
    """
    issue = await _visible_issue(request, key)
    qs = issue.history.select_related("actor")
    if field:
        qs = qs.filter(field=field)
    return await paginate(qs, ChangelogOut.from_entry, page, size)


class AuditOut(Schema):
    id: int
    verb: str
    target_type: str
    target_id: int | None = None
    target_label: str = ""
    actor: str = ""
    created_at: str
    metadata: dict = {}

    @staticmethod
    def from_entry(a) -> AuditOut:
        return AuditOut(
            id=a.pk,
            verb=a.verb,
            target_type=a.target_type,
            target_id=a.target_id,
            target_label=a.target_label or "",
            actor=a.actor.username if a.actor_id else "",
            created_at=a.created_at.isoformat() if a.created_at else "",
            metadata=a.metadata or {},
        )


@api.get("/projects/{key}/activity/", response=Page[AuditOut])
async def project_activity(
    request, key: str, page: int = 1, size: int = DEFAULT_PAGE_SIZE, verb: str | None = None
):
    """Project-wide activity: creates, updates, deletes, with the actor.

    This is the complete record of what happened — signal-driven, so it does not
    miss a write the way ``HistoryEntry`` does — but it carries no before/after
    values, so it answers "who touched this and when", not "what did it say
    before". Use the changelog for that and this for the who and the when.

    Rows are dropped by ``purge_old_data`` after 90 days by default, and deleting
    the project deletes its own audit trail along with everything else, so this
    is a recent-history log and not an archive.
    """
    project = await _visible_project(request, key)
    qs = project.audit.select_related("actor")
    if verb:
        qs = qs.filter(verb=verb)
    return await paginate(qs, AuditOut.from_entry, page, size)


# --- project analytics ----------------------------------------------------
#
# SLA, burndown and reports exist as server-rendered pages and answer the
# questions a standup actually asks — what is stuck, how the sprint is going,
# how fast the team ships. They are computed views over the same rows the API
# already exposes, and recomputing them client-side from changelog pages is
# the kind of work that drifts: every caller would bucket weeks or clamp
# percentiles slightly differently. So the aggregation lives here, mirroring
# the web views query for query, and the response carries data rather than
# markup — no SVG coordinates, no template context.


class SlaItemOut(Schema):
    issue: str
    summary: str
    status: str
    priority: str = ""
    assignee: str = ""
    entered_at: str
    days_in_status: int


class SlaOut(Schema):
    project: str
    threshold_days: int
    count: int
    items: list[SlaItemOut]


@api.get("/projects/{key}/sla/", response=SlaOut)
async def project_sla(request, key: str, days: int = 7):
    """Open issues stuck in one status longer than a threshold.

    Same definition as the web view, which this mirrors: an issue's "time at
    current status" is the newest ``HistoryEntry`` with ``field="status"``, or
    the issue's own creation when it has never moved. Done-category issues are
    never stuck, archived ones are off the board, and the list is oldest first
    so the worst offender is at the top.
    """
    from issues.models import HistoryEntry, Issue

    project = await _visible_project(request, key)
    threshold = max(1, days)
    now = timezone.now()
    cutoff = now - timezone.timedelta(days=threshold)
    candidates = [
        i
        async for i in Issue.objects.filter(project=project, archived=False)
        .exclude(status__category="done")
        .select_related("status", "priority", "assignee")
        .order_by("updated_at")
    ]
    items = []
    for i in candidates:
        last_change = (
            await HistoryEntry.objects.filter(issue=i, field="status").order_by("-created_at").afirst()
        )
        entered_at = last_change.created_at if last_change else i.created_at
        if entered_at <= cutoff:
            items.append(
                SlaItemOut(
                    issue=i.key,
                    summary=i.summary,
                    status=str(i.status),
                    priority=str(i.priority),
                    assignee=i.assignee.username if i.assignee_id else "",
                    entered_at=entered_at.isoformat(),
                    days_in_status=int((now - entered_at).total_seconds() / 86400),
                )
            )
    items.sort(key=lambda r: -r.days_in_status)
    return SlaOut(project=project.key, threshold_days=threshold, count=len(items), items=items)


class BurndownPointOut(Schema):
    date: str
    # Story points the ideal line says should remain. Linear from the total on
    # day zero to zero on the last day — the same straight line the chart draws.
    ideal: float
    # Story points actually remaining. Null for future days, which have no
    # actual yet; a client that plots null as zero draws a cliff that is not
    # there.
    actual: float | None = None


class VelocityRowOut(Schema):
    name: str
    committed: int
    completed: int


class SprintRefOut(Schema):
    id: int
    name: str
    status: str
    start_date: str = ""
    end_date: str = ""


class BurndownOut(Schema):
    project: str
    sprint: SprintRefOut | None = None
    total_sp: int = 0
    done_sp: int = 0
    percent_done: float = 0
    points: list[BurndownPointOut] = []
    # Last twelve closed sprints, oldest first: committed (story points in the
    # sprint) vs completed (resolved inside its dates).
    velocity: list[VelocityRowOut] = []


@api.get("/projects/{key}/burndown/", response=BurndownOut)
async def project_burndown(request, key: str, sprint: int | None = None):
    """Sprint burndown plus recent velocity.

    Same sprint selection as the web view: the named sprint when given, else
    the active one, else the latest by start date. A project with no sprints
    answers with an empty chart rather than a 404 — "no sprints yet" is a
    state, not an error. Velocity covers the last twelve closed sprints; the
    SVG coordinates the template needs are deliberately absent, because a JSON
    client plots its own chart.
    """
    from datetime import timedelta

    from issues.models import Issue

    project = await _visible_project(request, key)
    current = None
    if sprint is not None:
        current = await project.sprints.filter(pk=sprint).afirst()
        if current is None:
            raise Http404
    if current is None:
        current = (
            await project.sprints.filter(status="active").afirst()
            or await project.sprints.order_by("-start_date").afirst()
        )

    closed = [s async for s in project.sprints.filter(status="closed").order_by("end_date")[:12]]
    closed_ids = [s.pk for s in closed]
    by_sprint: dict[int, list] = {sid: [] for sid in closed_ids}
    if closed_ids:
        async for i in Issue.objects.filter(sprint_id__in=closed_ids).only(
            "sprint_id", "story_points", "resolved_at"
        ):
            by_sprint[i.sprint_id].append(i)
    velocity = []
    for s in closed:
        rows = by_sprint.get(s.pk, [])
        committed = sum(i.story_points or 0 for i in rows)
        completed = sum(
            (i.story_points or 0)
            for i in rows
            if i.resolved_at
            and s.start_date
            and s.end_date
            and s.start_date <= i.resolved_at.date() <= s.end_date
        )
        velocity.append(VelocityRowOut(name=s.name, committed=committed, completed=completed))

    out = BurndownOut(project=project.key, velocity=velocity)
    if current is None:
        return out
    out.sprint = SprintRefOut(
        id=current.pk,
        name=current.name,
        status=current.status,
        start_date=current.start_date.isoformat() if current.start_date else "",
        end_date=current.end_date.isoformat() if current.end_date else "",
    )
    if current.start_date and current.end_date:
        rows = [i async for i in Issue.objects.filter(sprint=current).only("story_points", "resolved_at")]
        total = sum(i.story_points or 0 for i in rows)
        days = (current.end_date - current.start_date).days or 1
        today = timezone.localdate()
        points = []
        for n in range(days + 1):
            day = current.start_date + timedelta(days=n)
            remaining = sum(
                (i.story_points or 0) for i in rows if not (i.resolved_at and i.resolved_at.date() <= day)
            )
            points.append(
                BurndownPointOut(
                    date=day.isoformat(),
                    ideal=round(total - (total / days) * n, 1),
                    actual=remaining if day <= today else None,
                )
            )
        done = sum(i.story_points or 0 for i in rows if i.resolved_at)
        out.total_sp = total
        out.done_sp = done
        out.percent_done = round(done * 100.0 / total, 1) if total else 0
        out.points = points
    return out


class ThroughputWeekOut(Schema):
    week: str
    count: int


class CycleTimeOut(Schema):
    count: int
    median_h: float
    avg_h: float
    p90_h: float


class WipRowOut(Schema):
    name: str
    category: str
    count: int


class ReportsOut(Schema):
    project: str
    throughput: list[ThroughputWeekOut]
    throughput_max: int
    cycle: CycleTimeOut
    wip: list[WipRowOut]
    wip_max: int


@api.get("/projects/{key}/reports/", response=ReportsOut)
async def project_reports(request, key: str):
    """Throughput, cycle time and a WIP-by-status snapshot.

    Same three blocks as the web view, same windows: resolved-per-ISO-week for
    the last eight weeks, cycle time over issues resolved in the last ninety
    days, WIP as a per-status snapshot. Two caveats travel with the numbers
    because they are load-bearing. The cycle-time start is the first recorded
    status change, falling back to the issue's creation — and like every
    changelog built on ``HistoryEntry``, that history is incomplete by
    construction, so cycle time is a lower bound as much as a measurement. And
    WIP counts every issue in each status, archived included, exactly as the
    page does: filtering one and not the other would make the two disagree.
    """
    from datetime import timedelta

    from issues.models import HistoryEntry, Issue, Status

    project = await _visible_project(request, key)
    now = timezone.now()

    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    weeks_back = 8
    first_week = week_start - timedelta(weeks=weeks_back - 1)
    buckets = {first_week + timedelta(weeks=w): 0 for w in range(weeks_back)}
    async for i in Issue.objects.filter(project=project, resolved_at__gte=first_week).only("resolved_at"):
        bucket = (i.resolved_at - timedelta(days=i.resolved_at.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if bucket in buckets:
            buckets[bucket] += 1
    throughput = [
        ThroughputWeekOut(week=week.strftime("%Y-W%V"), count=count)
        for week, count in sorted(buckets.items())
    ]

    recent = [
        i
        async for i in Issue.objects.filter(project=project, resolved_at__gte=now - timedelta(days=90)).only(
            "id", "resolved_at", "created_at"
        )
    ]
    recent_ids = [i.pk for i in recent]
    starts: dict[int, Any] = {}
    if recent_ids:
        async for h in (
            HistoryEntry.objects.filter(issue_id__in=recent_ids, field="status")
            .order_by("issue_id", "created_at")
            .only("issue_id", "created_at")
        ):
            if h.issue_id not in starts:
                starts[h.issue_id] = h.created_at
    durations = []
    for i in recent:
        start = starts.get(i.pk, i.created_at)
        if i.resolved_at and start and i.resolved_at > start:
            durations.append((i.resolved_at - start).total_seconds() / 3600.0)
    if durations:
        durations.sort()
        mid = len(durations) // 2
        median = durations[mid] if len(durations) % 2 else (durations[mid - 1] + durations[mid]) / 2
        avg = sum(durations) / len(durations)
        p90 = durations[int(0.9 * (len(durations) - 1))]
    else:
        median = avg = p90 = 0
    cycle = CycleTimeOut(
        count=len(durations), median_h=round(median, 1), avg_h=round(avg, 1), p90_h=round(p90, 1)
    )

    wip = []
    async for s in Status.objects.order_by("order").all():
        wip.append(
            WipRowOut(
                name=s.name,
                category=s.category,
                count=await Issue.objects.filter(project=project, status=s).acount(),
            )
        )
    return ReportsOut(
        project=project.key,
        throughput=throughput,
        throughput_max=max((t.count for t in throughput), default=0) or 1,
        cycle=cycle,
        wip=wip,
        wip_max=max((w.count for w in wip), default=0) or 1,
    )


# --- attachments ---
#
# Stored fully in the database as base64 in a TextField, capped at 5 MB, which is
# why CSP needs frame-src data:. Exposing them is a read and a write of bytes, so
# the upload takes base64 rather than a multipart body: a JSON API cannot carry a
# file otherwise, and asking a client to encode one is a smaller step than
# teaching this endpoint multipart.


class AttachmentOut(Schema):
    id: int
    filename: str
    content_type: str
    size: int
    uploaded_by: str = ""
    uploaded_at: str

    @staticmethod
    def from_attachment(a) -> AttachmentOut:
        return AttachmentOut(
            id=a.pk,
            filename=a.filename,
            content_type=a.content_type,
            size=a.size,
            uploaded_by=a.uploaded_by.username if a.uploaded_by_id else "",
            uploaded_at=a.uploaded_at.isoformat() if a.uploaded_at else "",
        )


class AttachmentIn(Schema):
    filename: str = Field(max_length=255)
    #: Base64 without a data: prefix, which the UI builds for its own <img> tags
    #: and which is not part of the file.
    data: str
    content_type: str = Field(default="application/octet-stream", max_length=120)


MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024


@api.get("/issues/{key}/attachments/", response=Page[AttachmentOut])
async def list_attachments(request, key: str, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    """List an issue's attachments. The bytes are not included; ask for one by id.

    Listing is separate from fetching because the common case is "does this issue
    have a screenshot on it", and a page of 5 MB base64 blobs would be 6.7 MB of
    response to answer it.
    """
    issue = await _visible_issue(request, key)
    qs = issue.attachments.select_related("uploaded_by").order_by("uploaded_at")
    return await paginate(qs, AttachmentOut.from_attachment, page, size)


@api.get("/attachments/{attachment_id}/", response=dict)
async def get_attachment(request, attachment_id: int):
    """Fetch one attachment's bytes.

    The body is a data: URL, the same shape the web UI uses for a download link,
    so a client that can render an <img> can render this without decoding.
    """
    from issues.models import Attachment

    row = await Attachment.objects.filter(pk=attachment_id).select_related("issue", "uploaded_by").afirst()
    if row is None:
        raise Http404
    # Through the issue, so an attachment on an issue the caller cannot see is a
    # 404 rather than a 200 with the file in it.
    await _visible_issue(request, row.issue.key)
    return {
        **AttachmentOut.from_attachment(row).dict(),
        "issueIdOrKey": row.issue.key,
        "dataUrl": row.data_url,
    }


@api.post("/issues/{key}/attachments/", response=AttachmentOut)
async def add_attachment(request, key: str, payload: AttachmentIn):
    import base64
    import binascii

    from issues.models import Attachment

    issue = await _visible_issue(request, key)
    await _assert_can_edit(request, issue.project)
    filename = payload.filename.strip()
    if not filename:
        from ninja.errors import HttpError

        raise HttpError(400, "filename requerido")
    try:
        raw = base64.b64decode(payload.data, validate=True)
    except (binascii.Error, ValueError) as exc:
        # Decoding is the caller's mistake and an unhandled one would be a 500
        # rather than a 400, so it is named rather than left to propagate.
        from ninja.errors import HttpError

        raise HttpError(400, "data no es base64 válido") from exc
    if len(raw) > MAX_ATTACHMENT_BYTES:
        from ninja.errors import HttpError

        # 400 rather than 413: django-ninja's handler only maps a fixed set of
        # codes onto the {"detail": ...} envelope, and 413 is not one of them, so
        # it came back as Django's HTML 400 page. That is not an API error and
        # a client cannot parse it. The status asked for is in the message.
        raise HttpError(
            400,
            f"el adjunto pesa {len(raw)} bytes y el máximo son {MAX_ATTACHMENT_BYTES}; "
            "reduce el archivo o súbelo troceado",
        )
    row = await Attachment.objects.acreate(
        issue=issue,
        filename=filename,
        content_type=payload.content_type,
        size=len(raw),
        data=payload.data,
        uploaded_by=request.user,
    )
    return AttachmentOut.from_attachment(row)


@api.delete("/attachments/{attachment_id}/", response=dict)
async def delete_attachment(request, attachment_id: int):
    """Remove an attachment. Irreversible: the bytes are only here."""
    from issues.models import Attachment

    row = await Attachment.objects.filter(pk=attachment_id).select_related("issue").afirst()
    if row is None:
        raise Http404
    issue = await _visible_issue(request, row.issue.key)
    await _assert_can_edit(request, issue.project)
    await row.adelete()
    return {"deleted": attachment_id}


# --- notifications ---


class NotificationOut(Schema):
    id: int
    kind: str
    text: str
    url: str = ""
    read: bool = False
    actor: str = ""
    created_at: str

    @staticmethod
    def from_notification(n) -> NotificationOut:
        return NotificationOut(
            id=n.pk,
            kind=n.kind,
            text=n.text,
            url=n.url or "",
            read=n.read,
            actor=n.actor.username if n.actor_id else "",
            created_at=n.created_at.isoformat() if n.created_at else "",
        )


@api.get("/notifications/", response=Page[NotificationOut])
async def list_notifications(
    request, page: int = 1, size: int = DEFAULT_PAGE_SIZE, unread_only: bool = False
):
    """The caller's own notifications, newest first. Never anybody else's."""
    qs = Notification.objects.filter(recipient=request.user).select_related("actor")
    if unread_only:
        qs = qs.filter(read=False)
    return await paginate(qs, NotificationOut.from_notification, page, size)


class MarkReadIn(Schema):
    #: Ids to mark. Omit or send an empty list to mark every unread notification.
    #:
    #: A schema rather than a bare ``ids: list[int] | None`` parameter, because
    #: django-ninja reads a list-typed parameter as a *query* parameter, so the
    #: body was ignored and the endpoint answered 422 to every call that sent one.
    ids: list[int] | None = None


@api.post("/notifications/read/", response=dict)
async def mark_notifications_read(request, payload: MarkReadIn | None = None):
    """Mark notifications read. With no ids, marks all of them.

    Scoped to the caller's own rows in the query, so a list of somebody else's
    ids is a no-op rather than a way to clear their inbox.
    """
    qs = Notification.objects.filter(recipient=request.user, read=False)
    if payload is not None and payload.ids:
        qs = qs.filter(pk__in=payload.ids)
    # aupdate rather than a per-row save: a notification has no post_save
    # receivers, this is a bulk flag rather than a domain change, and the
    # alternative is one save per unread row. It also skips the four receivers
    # AGENTS.md warns about, which is right here — none of them is about a read
    # flag, and a notification going unread-to-read is not something anybody is
    # watching a board for.
    marked = await qs.aupdate(read=True)
    return {"marked_read": marked}


# --- API keys ---
#
# A key can be created, listed and revoked over HTTP, which it could not: an
# agent that had been handed one could not mint a second for another integration
# nor revoke one it had leaked.


class APIKeyOut(Schema):
    id: int
    name: str
    prefix: str
    created_at: str
    last_used_at: str = ""
    revoked_at: str = ""
    active: bool = False

    @staticmethod
    def from_key(k) -> APIKeyOut:
        return APIKeyOut(
            id=k.pk,
            name=k.name,
            prefix=k.prefix,
            created_at=k.created_at.isoformat() if k.created_at else "",
            last_used_at=k.last_used_at.isoformat() if k.last_used_at else "",
            revoked_at=k.revoked_at.isoformat() if k.revoked_at else "",
            active=k.revoked_at is None,
        )


class APIKeyIn(Schema):
    name: str = Field(max_length=80)


@api.get("/api-keys/", response=Page[APIKeyOut])
async def list_api_keys(request, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    """The caller's own keys. The secret is never returned: only its prefix."""
    qs = APIKey.objects.filter(owner=request.user).order_by("-created_at")
    return await paginate(qs, APIKeyOut.from_key, page, size)


@api.post("/api-keys/", response=dict)
async def create_api_key(request, payload: APIKeyIn):
    """Mint a key for the caller. The plaintext is in the response and nowhere else.

    A one-time secret: the database keeps a hash, so this is the only moment the
    token exists as text. The response says so, because a client that drops it has
    to mint another rather than look for it.
    """
    name = payload.name.strip()
    if not name:
        from ninja.errors import HttpError

        raise HttpError(400, "name requerido")
    key, plaintext = await APIKey.acreate_for(owner=request.user, name=name)
    return {
        "id": key.pk,
        "name": key.name,
        "prefix": key.prefix,
        "token": plaintext,
        "warning": (
            "This is the only time the token is shown; jirrabit stores a hash of it. "
            "Store it now — it cannot be retrieved later, only replaced."
        ),
    }


@api.delete("/api-keys/{key_id}/", response=dict)
async def revoke_api_key(request, key_id: int):
    """Revoke one of the caller's keys. Reversible in the sense that nothing is lost.

    Sets ``revoked_at`` rather than deleting the row, matching the web UI, so the
    key stays visible in the listing as inactive instead of vanishing — a key that
    silently disappeared would leave a client unable to tell "I revoked this" from
    "someone deleted my account's keys".
    """
    key = await APIKey.objects.filter(pk=key_id, owner=request.user).afirst()
    if key is None:
        raise Http404
    if key.revoked_at is None:
        key.revoked_at = timezone.now()
        await key.asave(update_fields=["revoked_at"])
    return {"revoked": key_id}


# --- teams ---
#
# A team is what a `@team:oncall` mention resolves to. Without this an agent
# cannot turn one into recipients, so a mention in a comment is a string it can
# neither verify nor expand.


class TeamOut(Schema):
    id: int
    slug: str
    name: str
    description: str = ""
    members: list[str] = []
    created_at: str


class TeamIn(Schema):
    slug: str = Field(max_length=40)
    name: str = Field(max_length=120)
    description: str = Field(default="", max_length=255)
    members: list[str] = []


@api.get("/teams/", response=Page[TeamOut])
async def list_teams(request, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    """Every team, with its member usernames. Any authenticated caller may read.

    Unlike a project's membership this is not a permission boundary — a team is a
    broadcast list, and who is on it is not secret to anyone who can mention it.
    """
    qs = Team.objects.order_by("name")
    return await paginate(qs, _team_out, page, size)


@api.post("/teams/", response=TeamOut)
async def create_team(request, payload: TeamIn):
    from ninja.errors import HttpError

    if not await _is_admin(request.user):
        raise HttpError(403, "Requiere superusuario")
    team = await _create_team(payload, request.user)
    if team is None:
        raise HttpError(404, "algún miembro no existe o está inactivo")
    return team


@api.patch("/teams/{team_id}/", response=TeamOut)
async def patch_team(request, team_id: int, payload: TeamIn):
    from ninja.errors import HttpError

    team = await Team.objects.filter(pk=team_id).afirst()
    if team is None:
        raise Http404
    slug = payload.slug.strip()
    name = payload.name.strip()
    if not slug or not name:
        raise HttpError(400, "slug y name son requeridos")
    if not await _is_admin(request.user):
        raise HttpError(403, "Requiere superusuario")
    # Renaming a team onto another team's slug is the same unique-constraint
    # collision as creating a duplicate, so it is checked for the same reason.
    if slug != team.slug and await Team.objects.filter(slug=slug).aexists():
        raise HttpError(409, f"ya existe un equipo con el slug {slug!r}")
    members = await _resolve_usernames(payload.members)
    if members is None:
        raise HttpError(404, "algún miembro no existe o está inactivo")
    team.slug = slug
    team.name = name
    team.description = payload.description
    await team.asave()
    await team.members.aset(members)
    return await _team_out(team)


@api.delete("/teams/{team_id}/", response=dict)
async def delete_team(request, team_id: int):
    """Delete a team. Superuser only.

    Reversible in the only sense that matters: no issue, comment or history row
    refers to a team, so nothing that already exists changes.
    """
    from ninja.errors import HttpError

    if not await _is_admin(request.user):
        raise HttpError(403, "Requiere superusuario")
    team = await Team.objects.filter(pk=team_id).afirst()
    if team is None:
        raise Http404
    await team.adelete()
    return {"deleted": team_id}


async def _team_out(team) -> TeamOut:
    # prefetched by the caller's queryset, so this is a read of loaded rows
    # rather than a query per team.
    members = [u.username async for u in team.members.all()]
    members.sort()
    return TeamOut(
        id=team.pk,
        slug=team.slug,
        name=team.name,
        description=team.description or "",
        members=members,
        created_at=team.created_at.isoformat() if team.created_at else "",
    )


async def _resolve_usernames(names):
    """Resolve usernames to users, or None if any of them is unknown.

    All-or-nothing on purpose: a team created with three of its four members
    would look right and quietly fail to mention somebody.
    """
    out = []
    for raw in names:
        user = await User.objects.filter(username=str(raw).strip(), is_active=True).afirst()
        if user is None:
            return None
        out.append(user)
    return out


async def _create_team(payload: TeamIn, actor) -> TeamOut | None:
    slug = payload.slug.strip()
    name = payload.name.strip()
    if not slug or not name:
        from ninja.errors import HttpError

        raise HttpError(400, "slug y name son requeridos")
    # Team.slug is unique, so without this the insert raises IntegrityError and
    # the caller gets a 500 with a traceback for what is a duplicate name. The
    # same trap that put length limits on every create schema.
    if await Team.objects.filter(slug=slug).aexists():
        from ninja.errors import HttpError

        raise HttpError(409, f"ya existe un equipo con el slug {slug!r}")
    members = await _resolve_usernames(payload.members)
    if members is None:
        return None
    team = await Team.objects.acreate(slug=slug, name=name, description=payload.description)
    if members:
        await team.members.aset(members)
    return await _team_out(team)


async def _is_admin(user) -> bool:
    from core.permissions import is_super

    return is_super(user)


# --- user administration ---


class UserAdminIn(Schema):
    username: str
    email: str = ""
    display_name: str = ""
    is_superuser: bool = False
    is_active: bool = True


class UserAdminPatch(Schema):
    email: str | None = None
    display_name: str | None = None
    is_superuser: bool | None = None
    is_active: bool | None = None


@api.get("/admin/users/", response=Page[UserOut])
async def list_users_admin(request, page: int = 1, size: int = DEFAULT_PAGE_SIZE, q: str | None = None):
    """Every user, for a superuser.

    The counterpart to ``/users/search/``, which any caller may use and which
    reports only what an assignee lookup needs. This one includes privilege flags,
    so it is superuser-only — a user directory that leaked ``is_superuser`` would
    tell an attacker who to target.
    """
    from ninja.errors import HttpError

    if not request.user.is_superuser:
        raise HttpError(403, "Requiere superusuario")
    qs = User.objects.order_by("username")
    if q:
        qs = qs.filter(models.Q(username__icontains=q) | models.Q(email__icontains=q))
    return await paginate(qs, lambda u: UserOut.from_orm(u), page, size)


@api.post("/admin/users/", response=dict)
async def create_user_admin(request, payload: UserAdminIn):
    """Create a user. Superuser only.

    No password is set: the account is created inactive-or-active as asked but
    cannot sign in until somebody goes through the invite flow. Accepting a
    password here would mean a second way to set one, and this endpoint is about
    directory management rather than authentication.
    """
    import secrets

    from ninja.errors import HttpError

    if not request.user.is_superuser:
        raise HttpError(403, "Requiere superusuario")
    username = payload.username.strip()
    if not username:
        raise HttpError(400, "username requerido")
    if await User.objects.filter(username=username).aexists():
        raise HttpError(409, f"ya existe un usuario llamado {username!r}")
    user = await User.objects.acreate(
        username=username,
        email=payload.email,
        display_name=payload.display_name,
        is_superuser=payload.is_superuser,
        is_active=payload.is_active,
    )
    # An unusable password, so the account exists for membership purposes and
    # cannot be signed into until a reset is issued. Set rather than left blank
    # because an empty password is a valid password in some checks.
    user.set_password(secrets.token_urlsafe(32))
    await user.asave(update_fields=["password"])
    return {"id": user.pk, "username": user.username, "usable_password": False}


@api.patch("/admin/users/{user_id}/", response=UserOut)
async def patch_user_admin(request, user_id: int, payload: UserAdminPatch):
    """Edit a user. Superuser only.

    ``is_active`` is the soft delete: jirrabit never calls ``User.delete``, and
    ``AbstractUser.is_active`` says as much in its own help text. Deactivating
    keeps every issue, comment and worklog the person wrote, attributed to them.
    """
    from ninja.errors import HttpError

    if not request.user.is_superuser:
        raise HttpError(403, "Requiere superusuario")
    user = await User.objects.filter(pk=user_id).afirst()
    if user is None:
        raise Http404
    for field, value in payload.dict(exclude_unset=True).items():
        setattr(user, field, value)
    await user.asave()
    return UserOut.from_orm(user)


@api.delete("/admin/users/{user_id}/", response=dict)
async def delete_user_admin(request, user_id: int):
    """Deactivate a user rather than deleting the row.

    Named delete because that is what the caller asked for, and it is a soft one:
    ``Issue.reporter`` and ``Issue.assignee`` are on_delete=PROTECT, so a real
    delete would fail outright on any instance where the person has ever filed
    an issue. Deactivating revokes access and keeps the history.
    """
    from ninja.errors import HttpError

    if not request.user.is_superuser:
        raise HttpError(403, "Requiere superusuario")
    if user_id == request.user.pk:
        raise HttpError(400, "no puedes desactivar tu propia cuenta")
    user = await User.objects.filter(pk=user_id).afirst()
    if user is None:
        raise Http404
    user.is_active = False
    await user.asave(update_fields=["is_active"])
    return {"deactivated": user_id, "deleted": False}


@api.get("/labels/", response=Page[LabelOut])
async def list_labels(request, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    """Every label on the instance. Labels are global, not per project."""
    qs = Label.objects.order_by("name")
    return await paginate(qs, lambda lb: LabelOut.from_orm(lb), page, size)


@api.post("/labels/", response=LabelOut)
async def create_label(request, payload: LabelIn):
    name = payload.name.strip()
    if not name:
        from ninja.errors import HttpError

        raise HttpError(400, "name requerido")
    label, created = await Label.objects.aget_or_create(name=name, defaults={"color": payload.color})
    if not created:
        # Idempotent rather than an error: setting a label on an issue creates it
        # on demand, so a client that creates one explicitly and then sets it must
        # not be told it lost a race with itself.
        return LabelOut.from_orm(label)
    return LabelOut.from_orm(label)


@api.delete("/labels/{label_id}/", response=dict)
async def delete_label(request, label_id: int):
    """Delete a label. Superuser only, and refuses one still in use.

    ``Issue.labels`` is a many-to-many, so a delete does **not** fail when the
    label is in use: it removes the join rows and the label quietly disappears
    from every issue carrying it, which is the opposite of what "delete this
    label" means to whoever asked. Nothing at the database level objects.

    So the delete itself is conditional — one statement carrying the
    ``issues__isnull`` test, which is what makes it atomic. Counting first and
    deleting afterwards would leave a window where an issue labelled in between
    loses the label without anybody being told, and a window like that is
    exactly the kind of silent data loss this endpoint exists to prevent.
    """
    if not request.user.is_superuser:
        from ninja.errors import HttpError

        raise HttpError(403, "Requiere superusuario")
    if not await Label.objects.filter(pk=label_id).aexists():
        raise Http404
    in_use = await Issue.objects.filter(labels__pk=label_id).acount()
    deleted, _ = await Label.objects.filter(pk=label_id, issues__isnull=True).adelete()
    if not deleted:
        from ninja.errors import HttpError

        raise HttpError(409, f"la etiqueta está en uso por {in_use} tareas; retírala antes de borrarla")
    return {"deleted": label_id}


# --- epics ---
#
# JQL could filter on `epic` from the day it shipped while nothing in the API
# could read or write an epic, so a search would return issues whose epic the
# caller had no way to learn and no way to change. It is the largest hole in the
# write surface: every planning question an agent asks starts with "which epic".


class EpicOut(ModelSchema):
    class Meta:
        model = Epic
        fields = ["id", "name", "summary", "color", "done", "created_at"]


class EpicIn(Schema):
    name: str = Field(max_length=200)
    summary: str = ""
    color: str = Field(default="#1e6fff", max_length=20)
    done: bool = False


class EpicPatch(Schema):
    name: str | None = Field(default=None, max_length=200)
    summary: str | None = None
    color: str | None = Field(default=None, max_length=20)
    done: bool | None = None


@api.get("/projects/{key}/epics/", response=Page[EpicOut])
async def list_epics(request, key: str, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    project = await _visible_project(request, key)
    qs = project.epics.order_by("-created_at")
    return await paginate(qs, lambda e: EpicOut.from_orm(e), page, size)


@api.post("/projects/{key}/epics/", response=EpicOut)
async def create_epic(request, key: str, payload: EpicIn):
    project = await _visible_project(request, key)
    await _assert_project_admin(request, project)
    name = payload.name.strip()
    if not name:
        from ninja.errors import HttpError

        raise HttpError(400, "name requerido")
    epic = await Epic.objects.acreate(
        project=project,
        name=name,
        summary=payload.summary,
        color=payload.color,
        done=payload.done,
        created_by=request.user,
    )
    return EpicOut.from_orm(epic)


@api.get("/projects/{key}/epics/{epic_id}/", response=EpicOut)
async def get_epic(request, key: str, epic_id: int):
    project = await _visible_project(request, key)
    return await _visible_epic(project, epic_id)


@api.patch("/projects/{key}/epics/{epic_id}/", response=EpicOut)
async def patch_epic(request, key: str, epic_id: int, payload: EpicPatch):
    project = await _visible_project(request, key)
    await _assert_project_admin(request, project)
    epic = await _visible_epic(project, epic_id)
    for field, value in payload.dict(exclude_unset=True).items():
        setattr(epic, field, value.strip() if field == "name" and value else value)
    await epic.asave()
    return EpicOut.from_orm(epic)


@api.delete("/projects/{key}/epics/{epic_id}/", response=dict)
async def delete_epic(request, key: str, epic_id: int):
    """Delete an epic. Its issues are kept and become unassigned.

    Not the two-step group on the MCP side: the rows an epic owns are the epic
    and nothing else, because ``Issue.epic`` is ``on_delete=SET_NULL`` — which is
    also why this is a small operation compared to deleting an issue.
    """
    project = await _visible_project(request, key)
    await _assert_project_admin(request, project)
    epic = await _visible_epic(project, epic_id)
    detached = await Issue.objects.filter(epic_id=epic.pk).acount()
    await epic.adelete()
    return {"deleted": epic_id, "issues_left_unassigned": detached}


async def _visible_epic(project: Project, epic_id: int) -> Epic:
    """An epic of a project the caller can see, or 404.

    The project is resolved by the caller, so this only has to check membership
    of it — the same reasoning as _resolve_issue_key.
    """
    try:
        return await Epic.objects.aget(pk=epic_id, project=project)
    except Epic.DoesNotExist as exc:
        raise Http404 from exc


@api.post("/projects/{key}/issues/", response=IssueOut)
async def create_issue(request, key: str, payload: IssueIn):
    from ninja.errors import HttpError

    project = await _visible_project(request, key)
    try:
        status = (
            await Status.objects.aget(pk=payload.status_id)
            if payload.status_id
            else await Status.objects.order_by("order").afirst()
        )
        priority = (
            await Priority.objects.aget(pk=payload.priority_id)
            if payload.priority_id
            else await Priority.objects.afirst()
        )
        itype = (
            await IssueType.objects.aget(pk=payload.issue_type_id)
            if payload.issue_type_id
            else await IssueType.objects.afirst()
        )
    except (Status.DoesNotExist, Priority.DoesNotExist, IssueType.DoesNotExist) as exc:
        raise HttpError(400, "status/priority/type inválido") from exc
    assignee_id = await _validate_assignee(project, payload.assignee_id)
    sprint_id = await _validate_sprint(project, payload.sprint_id)
    epic_id = await _validate_epic(project, payload.epic_id)
    parent_id = await _resolve_issue_key(project, payload.parent)
    labels = await _validate_labels(payload.labels) if payload.labels else []
    issue = await Issue.objects.acreate(
        project=project,
        reporter=request.user,
        summary=payload.summary,
        description=payload.description,
        status=status,
        priority=priority,
        issue_type=itype,
        assignee_id=assignee_id,
        sprint_id=sprint_id,
        epic_id=epic_id,
        parent_id=parent_id,
        story_points=payload.story_points,
        due_date=payload.due_date,
        estimate_minutes=payload.estimate_minutes,
        time_remaining_minutes=payload.time_remaining_minutes,
    )
    if labels:
        # aset, not aupdate: the M2M through-table has no post_save of its own
        # and the row was already saved above, so this is the same call the board
        # makes when it adds a label.
        await issue.labels.aset(labels)
    # Re-read rather than serialising the row just built. afrom_issue reads
    # i.parent.key and i.epic, and an acreate'd instance has no cached relation
    # for either — so those reads are synchronous queries on the event loop, which
    # raise SynchronousOnlyOperation. _visible_issue joins them, and re-fetching
    # is what the status path below already does for the same reason.
    return await IssueOut.afrom_issue(await _visible_issue(request, issue.key))


class CloneIn(Schema):
    # Default summary when omitted: the UI prefixes "[clon] ", and the API keeps
    # the same spelling so a clone is recognisable wherever it was made.
    summary: str | None = Field(default=None, max_length=255)
    # Place the clone straight into a sprint: "clone this for next sprint" is
    # the workflow this endpoint exists for.
    sprint_id: int | None = None
    # Copy direct subtasks as children of the clone, one level only. Off by
    # default: cloning a parent with twenty children is a bulk create wearing a
    # single-issue costume, and the caller should ask for it out loud.
    include_subtasks: bool = False


class CloneOut(Schema):
    issue: IssueOut
    # Keys of the cloned subtasks, in the same order as the originals. Empty
    # unless include_subtasks was set.
    subtasks: list[str] = []


@api.post("/issues/{key}/clone/", response=CloneOut)
async def clone_issue(request, key: str, payload: CloneIn):
    """Duplicate an issue: summary, description, type, priority, assignee,
    labels, epic, story points, estimate and due date.

    Same field set as the web UI's clone, which this mirrors on purpose so the
    two cannot disagree about what "a copy" means. Subtasks, comments, history,
    attachments, links and logged time are never copied — the clone starts
    fresh — and neither is the archived flag: a copy of an archived issue is an
    active issue. The reporter is the caller, not the original reporter, because
    the person who asked for the copy owns it.
    """
    src = await _visible_issue(request, key)
    await _assert_can_edit(request, src.project)
    # Blank, whitespace-only and omitted all mean "the default prefix": summary
    # is required on the model, so the fallback can never be empty and there is
    # no dead 400 branch to maintain.
    summary = (payload.summary or "").strip() or f"[clon] {src.summary}"
    sprint_id = await _validate_sprint(src.project, payload.sprint_id)

    async def _copy(source: Issue, parent_id: int | None, summary: str, sprint_id: int | None) -> Issue:
        num = await source.project.anext_issue_number()
        clone = await Issue.objects.acreate(
            project=source.project,
            reporter=request.user,
            summary=summary[:255],
            description=source.description,
            status=source.status,
            priority=source.priority,
            issue_type=source.issue_type,
            assignee=source.assignee,
            epic=source.epic,
            parent_id=parent_id,
            sprint_id=sprint_id,
            story_points=source.story_points,
            estimate_minutes=source.estimate_minutes,
            due_date=source.due_date,
            key=f"{source.project.key}-{num}",
        )
        labels = [lab async for lab in source.labels.all()]
        if labels:
            # aset, not aupdate: same call the board makes, and the M2M
            # through-table has no receivers to skip.
            await clone.labels.aset(labels)
        return clone

    clone = await _copy(src, None, summary, sprint_id)
    subtasks: list[str] = []
    if payload.include_subtasks:
        # No sprint on the children: a subtask rides with its parent, and a
        # sprint id on both would double-book it wherever sprints are counted.
        # Same joins as _visible_issue, for the same reason: _copy reads every
        # relation on the row, and an uncached one is a synchronous query on
        # the event loop.
        children = [
            child
            async for child in Issue.objects.filter(parent=src)
            .select_related("project", "status", "priority", "issue_type", "assignee", "epic")
            .prefetch_related("labels")
            .order_by("rank", "-updated_at")
        ]
        for child in children:
            sub = await _copy(child, clone.pk, f"[clon] {child.summary}", None)
            subtasks.append(sub.key)
    # Re-read: acreate leaves relations uncached and afrom_issue reads parent,
    # epic and labels, which on the event loop would be synchronous queries.
    return CloneOut(
        issue=await IssueOut.afrom_issue(await _visible_issue(request, clone.key)),
        subtasks=subtasks,
    )


@api.get("/issues/{key}/", response=IssueOut)
async def get_issue(request, key: str):
    issue = await _visible_issue(request, key)
    return await IssueOut.afrom_issue(issue)


_PATCHABLE_FIELDS = {
    "summary",
    "description",
    "status_id",
    "priority_id",
    "issue_type_id",
    "assignee_id",
    "sprint_id",
    "epic_id",
    "story_points",
    "due_date",
    "archived",
    "estimate_minutes",
    "time_remaining_minutes",
}


@api.patch("/issues/{key}/", response=IssueOut)
async def patch_issue(request, key: str, payload: IssuePatch):
    from ninja.errors import HttpError

    args_issue_key = key
    issue = await _visible_issue(request, key)
    await _assert_can_edit(request, issue.project)
    data = payload.dict(exclude_unset=True)
    if "status_id" in data:
        if not await Status.objects.filter(pk=data["status_id"]).aexists():
            raise HttpError(400, "status inválido")
    if "priority_id" in data:
        if not await Priority.objects.filter(pk=data["priority_id"]).aexists():
            raise HttpError(400, "priority inválido")
    if "issue_type_id" in data:
        # It was missing from the allow-list entirely, so a PATCH carrying it was
        # validated by the schema, accepted, and then dropped by the loop below —
        # a 200 that changed nothing. Creating an issue with the same field worked
        # all along, because that path does not go through this list, and the
        # asymmetry is what hid it. Now that it is applied, it needs the same
        # existence check as its neighbours.
        if not await IssueType.objects.filter(pk=data["issue_type_id"]).aexists():
            raise HttpError(400, "tipo inválido")
    if "assignee_id" in data:
        await _validate_assignee(issue.project, data["assignee_id"])
    if "sprint_id" in data:
        await _validate_sprint(issue.project, data["sprint_id"])
    if "epic_id" in data:
        await _validate_epic(issue.project, data["epic_id"])
    # parent and labels are handled outside the loop below: one is a key that has
    # to be resolved to a row, and the other is a many-to-many. Both are popped
    # here so the setattr loop below cannot try to assign them.
    parent_id = data.pop("parent", _UNSET)
    if parent_id is not _UNSET:
        # A key rather than a pk, and the issue itself is excluded: an issue
        # cannot be its own parent, which is a cycle nothing would catch.
        parent_id = await _resolve_issue_key(issue.project, parent_id, exclude=issue)
    new_labels = data.pop("labels", _UNSET)
    resolved_labels = None
    if new_labels is not _UNSET:
        resolved_labels = await _validate_labels(new_labels)

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
        target_status = await Status.objects.prefetch_related("allowed_next").aget(pk=new_status_id)
        if not issue.status.can_transition_to(target_status):
            raise HttpError(400, "transición de estado no permitida por el workflow")

    # status_id is applied separately below, so drop it from the generic loop.
    data.pop("status_id", None)
    for field, value in data.items():
        if field not in _PATCHABLE_FIELDS:
            continue
        setattr(issue, field, value)
    if parent_id is not _UNSET:
        issue.parent_id = parent_id
    await issue.asave()
    if resolved_labels is not None:
        # aset replaces the set, which is what a PATCH carrying the whole list
        # means. Reading the field as "add these" would make a label
        # unremovable, and the board's own label_remove action exists because the
        # API had no way to express it.
        await issue.labels.aset(resolved_labels)
        # The row save above predates the M2M change, so save once more for the
        # post_save receivers to see the final state and for updated_at to move
        # after the edit rather than before it.
        await issue.asave(update_fields=["updated_at"])

    # Any of these names a relation, and afrom_issue renders relations by name:
    # status, priority, issue_type, assignee, sprint, epic and parent. Assigning a
    # ``*_id`` leaves the cached object pointing at the old row, so serialising
    # this instance would report the previous value — a 200 that says "here is
    # what I did" and then describes something else. Re-read instead, which is
    # what the status path has always done for this reason.
    _FK_FIELDS = {
        "status_id",
        "priority_id",
        "issue_type_id",
        "assignee_id",
        "sprint_id",
        "epic_id",
    }
    if _FK_FIELDS & data.keys() or resolved_labels is not None or parent_id is not _UNSET:
        issue = await _visible_issue(request, args_issue_key)

    if new_status_id is not None and new_status_id != issue.status_id:
        from asgiref.sync import sync_to_async

        from issues.views import _change_status_atomic

        # sync_to_async, like issues.views and board.views already do for this
        # function. It holds a SELECT ... FOR UPDATE inside a transaction, and a
        # transaction has to be one unbroken block: an async rewrite would
        # release the row lock at every await. thread_sensitive keeps it on the
        # same thread as the rest of the request's database work.
        _, _, allowed = await sync_to_async(_change_status_atomic, thread_sensitive=True)(
            issue.pk, new_status_id, request.user.pk
        )
        if not allowed:
            raise HttpError(400, "transición de estado no permitida por el workflow")
        # Re-read the row rather than calling arefresh_from_db: the in-memory
        # copy predates the transition, so a later save() from it would write
        # the old status back over the new one, and arefresh_from_db drops the
        # prefetch cache, leaving status and issue_type unloaded and turning
        # serialisation into a synchronous query.
        issue = await _visible_issue(request, args_issue_key)

    return await IssueOut.afrom_issue(issue)


class DeletionImpactOut(Schema):
    """What a delete would take with it.

    A delete that only removes the named row is a lie, and the cascade is the
    part that matters: an issue takes its comments, worklogs, attachments, links
    in both directions, its watchers and *its subtasks* with it, because
    ``Issue.parent`` is ``on_delete=CASCADE``. A caller who is about to press the
    button has never been shown any of that, so this endpoint exists to make the
    cost legible before it is paid rather than after.

    Read-only and side-effect free, so it can be asked as often as needed.
    """

    key: str
    summary: str = ""
    project: str = ""
    status: str = ""
    comments: int = 0
    #: Includes soft-deleted comments, which are kept in the table.
    worklogs: int = 0
    attachments: int = 0
    #: Links where this issue is the source, and where it is the target. jirrabit
    #: stores both directions separately, so a delete removes one half and
    #: orphans the other unless the endpoint takes the inverse with it.
    links_out: int = 0
    links_in: int = 0
    subtasks: int = 0
    watchers: int = 0
    minutes_logged: int = 0
    #: Every row above, added up. A plain field rather than a property because a
    #: Schema only serialises declared fields, and a computed total is the number
    #: a caller actually reads.
    total: int = 0


@api.get("/issues/{key}/deletion-impact/", response=DeletionImpactOut)
async def issue_deletion_impact(request, key: str):
    issue = await _visible_issue(request, key)
    await _assert_can_edit(request, issue.project)
    from django.db.models import Sum

    from issues.models import Attachment

    comments = await Comment.objects.filter(issue=issue).acount()
    worklogs = await WorkLog.objects.filter(issue=issue).acount()
    minutes = await WorkLog.objects.filter(issue=issue).aaggregate(total=Sum("minutes"))
    attachments = await Attachment.objects.filter(issue=issue).acount()
    links_out = await IssueLink.objects.filter(source=issue).acount()
    links_in = await IssueLink.objects.filter(target=issue).acount()
    subtasks = await Issue.objects.filter(parent=issue).acount()
    body = DeletionImpactOut(
        key=issue.key,
        summary=issue.summary,
        project=issue.project.key,
        status=str(issue.status) if issue.status_id else "",
        comments=comments,
        worklogs=worklogs,
        attachments=attachments,
        links_out=links_out,
        links_in=links_in,
        subtasks=subtasks,
        watchers=await issue.watchers.acount(),
        minutes_logged=(minutes or {}).get("total") or 0,
    )
    body.total = (
        body.comments + body.worklogs + body.attachments + body.links_out + body.links_in + body.subtasks
    )
    return body


@api.delete("/issues/{key}/")
async def delete_issue(request, key: str):
    issue = await _visible_issue(request, key)
    await _assert_can_edit(request, issue.project)
    project, status_id = issue.project, issue.status_id
    await issue.adelete()
    # Closing the gap the card left in its board column. adelete() is a queryset
    # call, so it fires post_delete and nothing else, and there is no Issue
    # post_delete receiver that could renumber — without this the surviving
    # cards keep whatever ranks they held and the column stops being a dense
    # 0..n-1 run. Read from the in-memory issue, which outlives the row.
    from board.views import _densify_after_delete

    await _densify_after_delete(project, [status_id])
    return {"deleted": key}


@api.get("/issues/{key}/comments/", response=Page[CommentOut])
async def list_comments(
    request, key: str, page: int = 1, size: int = DEFAULT_PAGE_SIZE, include_deleted: bool = False
):
    issue = await _visible_issue(request, key)
    qs = issue.comments.select_related("author").order_by("created_at")
    # A soft-deleted comment is still a row, so without this it came back as if
    # it had never been removed — while `deleted_at` made it invisible in the web
    # UI. ?include_deleted=1 is how a client checks a deletion landed, and it is
    # also what makes DELETE idempotent to read back.
    if not include_deleted:
        qs = qs.filter(deleted_at__isnull=True)
    return await paginate(qs, CommentOut.from_comment, page, size)


@api.post("/issues/{key}/comments/", response=CommentOut)
async def add_comment(request, key: str, payload: CommentIn):
    issue = await _visible_issue(request, key)
    await _assert_can_edit(request, issue.project)
    c = await Comment.objects.acreate(issue=issue, author=request.user, body=payload.body)
    return CommentOut.from_comment(c)


# --- comment edit, soft delete and restore ---
#
# The web UI has had all three for a while (issues/views.py: CommentEditView,
# CommentDeleteView, CommentRestoreView) and the API had none of them, so an
# agent could add a comment and never take one back. Soft delete is the right
# default rather than a hard one: `deleted_at` keeps the body, which is what
# makes restore exact, and the row stays out of the default listing.


async def _comment_in_issue(request, key: str, comment_id: int) -> Comment:
    """Fetch a comment of ``key``, or 404.

    Scoped through the issue on purpose. A bare ``Comment.objects.aget(pk=...)``
    would answer 200 for a comment on an issue the caller cannot see, which is
    the leak the 404-instead-of-403 rule exists to prevent everywhere else.
    """
    issue = await _visible_issue(request, key)
    try:
        # select_related, not just the filter: CommentOut.from_comment reads
        # c.issue.key, and an unjoined relation there is a synchronous query in
        # the middle of an async view. Filtering by issue does not populate the
        # cache.
        return await Comment.objects.select_related("issue", "author").aget(pk=comment_id, issue=issue)
    except Comment.DoesNotExist as exc:
        raise Http404 from exc


class CommentEditIn(Schema):
    body: str


@api.patch("/issues/{key}/comments/{comment_id}/", response=CommentOut)
async def edit_comment(request, key: str, comment_id: int, payload: CommentEditIn):
    comment = await _comment_in_issue(request, key, comment_id)
    # Author-or-superuser, matching issues/views.py:501-503. Deliberately not
    # the project role: editing someone else's comment is an authorship claim,
    # not a permission the project's roles describe.
    if comment.author_id != request.user.pk and not request.user.is_superuser:
        from ninja.errors import HttpError

        raise HttpError(403, "Solo el autor o un superusuario puede editar el comentario")
    if comment.deleted_at is not None:
        from ninja.errors import HttpError

        raise HttpError(400, "El comentario está borrado; restáuralo antes de editarlo")
    new_body = payload.body.strip()
    if not new_body:
        from ninja.errors import HttpError

        raise HttpError(400, "body no puede quedar vacío")
    if new_body == comment.body:
        return CommentOut.from_comment(comment)
    # Snapshot first, so the history is a record of what the body was, and it
    # survives a rollback of the save that follows. Same order as the UI.
    from issues.models import CommentEdit

    await CommentEdit.objects.acreate(comment=comment, old_body=comment.body, edited_by=request.user)
    comment.body = new_body
    comment.edited = True
    await comment.asave()
    return CommentOut.from_comment(comment)


@api.delete("/issues/{key}/comments/{comment_id}/", response=CommentOut)
async def delete_comment(request, key: str, comment_id: int):
    """Soft-delete a comment. Reversible with the restore endpoint."""
    comment = await _comment_in_issue(request, key, comment_id)
    if comment.author_id != request.user.pk and not request.user.is_superuser:
        from ninja.errors import HttpError

        raise HttpError(403, "Solo el autor o un superusuario puede borrar el comentario")
    if comment.deleted_at is None:
        comment.deleted_at = timezone.now()
        # update_fields, not a full save: the fields it would rewrite are the
        # markdown cache, which is not what this call is about. Same call the
        # UI makes.
        await comment.asave(update_fields=["deleted_at", "updated_at"])
    return CommentOut.from_comment(comment)


@api.post("/issues/{key}/comments/{comment_id}/restore/", response=CommentOut)
async def restore_comment(request, key: str, comment_id: int):
    comment = await _comment_in_issue(request, key, comment_id)
    if comment.author_id != request.user.pk and not request.user.is_superuser:
        from ninja.errors import HttpError

        raise HttpError(403, "Solo el autor o un superusuario puede restaurar el comentario")
    if comment.deleted_at is not None:
        comment.deleted_at = None
        await comment.asave(update_fields=["deleted_at", "updated_at"])
    return CommentOut.from_comment(comment)


@api.get("/issues/{key}/comments/{comment_id}/history/", response=Page[CommentEditOut])
async def comment_history(request, key: str, comment_id: int, page: int = 1, size: int = DEFAULT_PAGE_SIZE):
    """The previous bodies of a comment, newest first.

    Incomplete by construction, like every changelog built on CommentEdit: only
    two code paths write one, so an edit made anywhere else leaves no trace.
    """
    comment = await _comment_in_issue(request, key, comment_id)
    qs = comment.edits.select_related("edited_by").order_by("-edited_at")
    return await paginate(qs, CommentEditOut.from_edit, page, size)


async def _assert_project_admin(request, project: Project) -> None:
    """Raise ninja HttpError(403) if the user isn't admin/lead on the project."""
    from ninja.errors import HttpError

    if request.user.is_superuser or project.lead_id == request.user.pk:
        return
    from projects.models import ProjectMembership

    is_admin = await ProjectMembership.objects.filter(
        project=project,
        user=request.user,
        role="admin",
    ).aexists()
    if not is_admin:
        raise HttpError(403, "Requiere rol admin en el proyecto")


async def _assert_can_edit(request, project: Project) -> None:
    """Raise ninja HttpError(403) if the user may only read the project.

    The mirror of :func:`_assert_project_admin` for writes that touch an issue
    rather than project settings, and the check the API was missing entirely.

    ``_visible_issue`` answers "can you see this", not "can you write this": it
    goes through ``Project.objects.filter_visible``, which admits a project
    membership of *any* role. Every issue-level write used to call it and
    nothing else, so a ``viewer`` could edit an issue, add a comment, log time
    and — the reason this exists — permanently delete an issue. The web UI was
    never affected: its views call ``core.permissions.aassert_can_edit``.

    ``can_edit`` is admin or member, matching ``core/permissions.py:43-44``,
    and a superuser or the project lead still pass. The 403 rather than 404 is
    also deliberate: the caller has already been shown the issue by
    ``_visible_issue``, so there is nothing left to hide, and 403 says the
    reason.
    """
    from ninja.errors import HttpError

    from core.permissions import aget_role, can_edit

    if not can_edit(await aget_role(request.user, project)):
        raise HttpError(403, "Requiere rol 'member' o superior en el proyecto")


@api.get("/me/", response=UserOut)
async def me(request):
    return request.user


class MePatch(Schema):
    display_name: str | None = Field(default=None, max_length=120)
    first_name: str | None = Field(default=None, max_length=150)
    last_name: str | None = Field(default=None, max_length=150)
    email: str | None = None
    job_title: str | None = Field(default=None, max_length=120)
    timezone: str | None = Field(default=None, max_length=64)
    language: str | None = Field(default=None, max_length=8)
    palette: str | None = Field(default=None, max_length=20)
    notify_email: bool | None = None
    muted_kinds: list[str] | None = None
    # A data URL (``data:image/png;base64,...``), like the profile form
    # produces. Empty string clears the avatar; omitted leaves it alone.
    avatar: str | None = None


class MeOut(Schema):
    id: int
    username: str
    display_name: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    job_title: str = ""
    timezone: str = ""
    language: str = ""
    palette: str = ""
    notify_email: bool = True
    muted_kinds: list[str] = []
    # The avatar itself is not echoed: it is a multi-megabyte base64 blob and
    # the caller just sent or cleared it, so a flag answers the only question
    # left, which is whether one is stored.
    has_avatar: bool = False

    @staticmethod
    def from_user(user) -> MeOut:
        return MeOut(
            id=user.pk,
            username=user.username,
            display_name=user.display_name or "",
            first_name=user.first_name or "",
            last_name=user.last_name or "",
            email=user.email or "",
            job_title=user.job_title or "",
            timezone=user.timezone or "",
            language=user.language or "",
            palette=user.palette or "",
            notify_email=user.notify_email,
            muted_kinds=[k for k in (user.muted_kinds or "").split(",") if k.strip()],
            has_avatar=bool(user.avatar),
        )


@api.patch("/me/", response=MeOut)
async def patch_me(request, payload: MePatch):
    """Edit the caller's own profile: the fields on the profile form.

    This is the self-service counterpart to ``PATCH /admin/users/{id}/``.
    Anything about identity or privilege is deliberately out of reach here:
    ``username`` cannot change (it is the login), there is no ``password``
    field (rotation stays in the web flow, where it belongs), and
    ``is_staff``/``is_superuser``/``is_active`` are admin-only. A caller that
    needs those is asking the wrong endpoint, not hitting a missing feature.
    """
    from ninja.errors import HttpError

    user = request.user
    data = payload.dict(exclude_unset=True)

    for field in ("display_name", "first_name", "last_name", "job_title"):
        if field in data and data[field] is not None:
            setattr(user, field, data[field].strip())

    if "email" in data and data["email"] is not None:
        from django.core.exceptions import ValidationError
        from django.core.validators import validate_email

        email = data["email"].strip()
        if email:
            try:
                validate_email(email)
            except ValidationError:
                raise HttpError(400, f"correo inválido: {email!r}") from None
        user.email = email

    if "timezone" in data and data["timezone"] is not None:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        tz = data["timezone"].strip()
        try:
            ZoneInfo(tz)
        except ZoneInfoNotFoundError:
            raise HttpError(400, f"zona horaria desconocida: {tz!r}") from None
        user.timezone = tz

    if "language" in data and data["language"] is not None:
        from django.conf import settings

        codes = [code for code, _name in settings.LANGUAGES]
        lang = data["language"].strip()
        if lang not in codes:
            raise HttpError(400, f"idioma inválido: {lang!r}; usa uno de {', '.join(codes)}")
        user.language = lang

    if "palette" in data and data["palette"] is not None:
        from core.palettes import palette_choices_simple

        slugs = [slug for slug, _label in palette_choices_simple()]
        palette = data["palette"].strip()
        if palette not in slugs:
            raise HttpError(400, f"paleta inválida: {palette!r}; usa una de {', '.join(slugs)}")
        user.palette = palette

    if "notify_email" in data and data["notify_email"] is not None:
        user.notify_email = data["notify_email"]

    if "muted_kinds" in data and data["muted_kinds"] is not None:
        valid = {key for key, _label in User.NOTIFY_KINDS}
        cleaned = []
        for kind in data["muted_kinds"]:
            kind = (kind or "").strip().lower()
            if kind not in valid:
                raise HttpError(
                    400, f"tipo de notificación inválido: {kind!r}; usa de {', '.join(sorted(valid))}"
                )
            if kind not in cleaned:
                cleaned.append(kind)
        user.muted_kinds = ",".join(cleaned)

    if "avatar" in data and data["avatar"] is not None:
        user.avatar = _check_avatar_data_url(data["avatar"])

    await user.asave()
    return MeOut.from_user(user)


def _check_avatar_data_url(value: str) -> str:
    """Validate an avatar data URL, returning the stored value.

    Mirrors the profile form's rules so the two cannot disagree: same MIME
    allowlist, same byte cap, same ``data:<mime>;base64,`` shape. Empty string
    clears the avatar, which is the API spelling of the form's "quitar avatar"
    checkbox.
    """
    import base64
    import binascii

    from ninja.errors import HttpError

    from accounts.forms import ALLOWED_AVATAR_MIME, MAX_AVATAR_BYTES

    if not value:
        return ""
    try:
        header, encoded = value.split(",", 1)
        mime = header.split(";")[0].split(":")[1]
    except ValueError, IndexError:
        raise HttpError(400, "avatar inválido: se espera data:image/...;base64,...") from None
    if mime not in ALLOWED_AVATAR_MIME:
        raise HttpError(400, f"tipo de imagen no soportado: {mime or 'desconocido'}")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except binascii.Error, ValueError:
        raise HttpError(400, "avatar inválido: el base64 no se puede decodificar") from None
    if len(raw) > MAX_AVATAR_BYTES:
        raise HttpError(400, "la imagen supera el tamaño máximo permitido")
    return value


# --- project mgmt ---

_PROJECT_PATCHABLE = {"name", "description", "archived"}


# --- project members ---
#
# Without this, an agent cannot know who it may assign work to. `assignee_id` has
# always been validated against membership — jirrabit rejects an assignee who is
# not in the project — but the list of who is in the project was only reachable
# through the web UI, so the check was a rule the client could not satisfy. That
# and the absence of a user directory were the two halves of "assigning is a
# guess".


class MemberOut(Schema):
    id: int
    username: str
    display_name: str = ""
    email: str = ""
    role: str
    is_lead: bool = False


class MemberIn(Schema):
    #: Username rather than a numeric id, matching the read side and the fact that
    #: a person is named, not numbered.
    username: str
    role: str = Field(default="member", max_length=16)


class MemberPatch(Schema):
    role: str


ROLES = ("admin", "member", "viewer")


async def _project_members(project: Project) -> list[MemberOut]:
    """Members and the lead as one list, lead first-class.

    The lead is not a ``ProjectMembership`` row — ``aget_role`` treats them as an
    admin without one — so a listing built from memberships alone would omit the
    one person who can always administer the project.
    """
    from projects.models import ProjectMembership

    rows = [m async for m in ProjectMembership.objects.filter(project=project).select_related("user")]
    out = [
        MemberOut(
            id=m.user_id,
            username=m.user.username,
            display_name=m.user.display_name or m.user.username,
            email=m.user.email,
            role=m.role,
            # The lead is often *also* a membership row, and that is the normal
            # case rather than the exception. So this is a flag on whichever row
            # it turns out to be, not a separate entry: appending the lead as an
            # extra row whenever no row matched would have left `is_lead` false on
            # exactly the configuration the product creates for itself.
            is_lead=m.user_id == project.lead_id,
        )
        for m in rows
    ]
    # The lead is a FK with on_delete=PROTECT, so they cannot have been removed
    # without replacing them, and they might not be a member at all.
    if project.lead_id and not any(m.id == project.lead_id for m in out):
        out.append(
            MemberOut(
                id=project.lead_id,
                username=project.lead.username,
                display_name=project.lead.display_name or project.lead.username,
                email=project.lead.email,
                role="admin",
                is_lead=True,
            )
        )
    out.sort(key=lambda m: (not m.is_lead, m.username))
    return out


@api.get("/projects/{key}/members/", response=list[MemberOut])
async def list_members(request, key: str):
    """Everyone who can see the project, with their role.

    A bare list and not a Page: a project's membership is a bounded set, and a
    caller paging through it would be a caller that has misunderstood it.
    """
    project = await _visible_project(request, key)
    return await _project_members(project)


@api.post("/projects/{key}/members/", response=MemberOut)
async def add_member(request, key: str, payload: MemberIn):
    from projects.models import ProjectMembership

    project = await _visible_project(request, key)
    await _assert_project_admin(request, project)
    if payload.role not in ROLES:
        from ninja.errors import HttpError

        raise HttpError(400, f"rol inválido; usa uno de {', '.join(ROLES)}")
    user = await User.objects.filter(username=payload.username, is_active=True).afirst()
    if user is None:
        from ninja.errors import HttpError

        raise HttpError(404, f"no hay ningún usuario activo llamado {payload.username!r}")
    if user.pk == project.lead_id:
        from ninja.errors import HttpError

        raise HttpError(409, "ese usuario es el responsable del proyecto y ya tiene permisos de admin")
    membership, created = await ProjectMembership.objects.aget_or_create(
        project=project, user=user, defaults={"role": payload.role}
    )
    if not created and membership.role != payload.role:
        # Re-adding somebody who is already a member is a role change, not an
        # error, so the response reflects what actually happened.
        membership.role = payload.role
        await membership.asave(update_fields=["role"])
    return MemberOut(
        id=user.pk,
        username=user.username,
        display_name=user.display_name or user.username,
        email=user.email,
        role=membership.role,
        is_lead=False,
    )


@api.patch("/projects/{key}/members/{user_id}/", response=MemberOut)
async def change_member_role(request, key: str, user_id: int, payload: MemberPatch):
    from projects.models import ProjectMembership

    project = await _visible_project(request, key)
    await _assert_project_admin(request, project)
    if payload.role not in ROLES:
        from ninja.errors import HttpError

        raise HttpError(400, f"rol inválido; usa uno de {', '.join(ROLES)}")
    if user_id == project.lead_id:
        from ninja.errors import HttpError

        raise HttpError(409, "el responsable del proyecto no se puede degradar; cambia el responsable antes")
    membership = (
        await ProjectMembership.objects.filter(project=project, user_id=user_id)
        .select_related("user")
        .afirst()
    )
    if membership is None:
        raise Http404
    membership.role = payload.role
    await membership.asave(update_fields=["role"])
    return MemberOut(
        id=membership.user_id,
        username=membership.user.username,
        display_name=membership.user.display_name or membership.user.username,
        email=membership.user.email,
        role=membership.role,
        is_lead=False,
    )


@api.delete("/projects/{key}/members/{user_id}/", response=dict)
async def remove_member(request, key: str, user_id: int):
    """Remove somebody from a project. Not the lead, who cannot be removed."""
    from projects.models import ProjectMembership

    project = await _visible_project(request, key)
    await _assert_project_admin(request, project)
    if user_id == project.lead_id:
        from ninja.errors import HttpError

        raise HttpError(409, "el responsable del proyecto no se puede quitar; cambia el responsable antes")
    removed, _ = await ProjectMembership.objects.filter(project=project, user_id=user_id).adelete()
    if not removed:
        raise Http404
    return {"removed": user_id}


# --- project creation ---


class ProjectCreateIn(Schema):
    #: 10, because the key is the issue-key prefix and Issue.key is built from it.
    key: str = Field(max_length=10)
    name: str = Field(max_length=120)
    description: str = ""
    #: The creator is the lead unless someone else is named, and the lead always
    #: resolves to admin — so naming somebody else without giving them a
    #: membership would leave a project its owner cannot administer.
    lead: str | None = None
    #: ``[(username, role)]``. Applied after creation, and a bad username fails
    #: the whole thing rather than leaving a half-built project.
    members: list[MemberIn] = []


@api.post("/projects/", response=ProjectOut)
async def create_project(request, payload: ProjectCreateIn):
    from ninja.errors import HttpError

    from projects.models import ProjectMembership

    key = payload.key.strip().upper()
    name = payload.name.strip()
    if not key:
        raise HttpError(400, "key requerido")
    if not name:
        raise HttpError(400, "name requerido")
    # Creating a project on somebody else's behalf is an administrative act, and
    # the lead is a PROTECT foreign key, so a typo in it is also a mess to undo.
    if payload.lead and not request.user.is_superuser:
        raise HttpError(403, "solo un superusuario puede crear un proyecto con otro responsable")
    lead = request.user
    if payload.lead:
        lead = await User.objects.filter(username=payload.lead, is_active=True).afirst()
        if lead is None:
            raise HttpError(404, f"no hay ningún usuario activo llamado {payload.lead!r}")
    if await Project.objects.filter(key=key).aexists():
        raise HttpError(409, f"ya existe un proyecto con la clave {key}")
    for member in payload.members:
        if member.role not in ROLES:
            raise HttpError(400, f"rol inválido {member.role!r}; usa uno de {', '.join(ROLES)}")

    project = await Project.objects.acreate(key=key, name=name, description=payload.description, lead=lead)
    for member in payload.members:
        user = await User.objects.filter(username=member.username, is_active=True).afirst()
        if user is None:
            # Nothing here is inside a transaction, so the project created above
            # would survive the refusal. Removing it keeps the promise that a
            # failed create leaves nothing behind: the key would otherwise be
            # taken by a project nobody can see, and its issue counter restarts
            # at zero.
            await project.adelete()
            raise HttpError(404, f"no hay ningún usuario activo llamado {member.username!r}")
        await ProjectMembership.objects.acreate(project=project, user=user, role=member.role)
    return project


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


class ProjectDeletionImpactOut(Schema):
    """What deleting a project would take with it.

    The largest cascade in the product, and the one hardest to reason about
    before the fact: every issue and therefore every comment, worklog,
    attachment, link and history row, plus the members, sprints, epics,
    webhooks, custom fields, wiki and issue templates.

    Two things it deliberately does not claim to be recoverable. The audit trail
    goes with the project, because ``AuditEntry.project`` is ``CASCADE`` and the
    audit receiver skips ``Project`` precisely so the delete does not 500 — so a
    deleted project leaves no record of itself anywhere. And the issue keys are
    globally unique, so recreating the project with the same key starts again at
    WEB-1 and the first issue created collides with one that already exists.
    """

    key: str
    name: str = ""
    archived: bool = False
    issues: int = 0
    members: int = 0
    sprints: int = 0
    epics: int = 0
    #: Everything above that is not an issue, added up.
    total: int = 0


@api.get("/projects/{key}/deletion-impact/", response=ProjectDeletionImpactOut)
async def project_deletion_impact(request, key: str):
    project = await _visible_project(request, key)
    await _assert_project_admin(request, project)
    from projects.models import Epic, ProjectMembership

    issues = await Issue.objects.filter(project=project).acount()
    members = await ProjectMembership.objects.filter(project=project).acount()
    sprints = await Sprint.objects.filter(project=project).acount()
    epics = await Epic.objects.filter(project=project).acount()
    return ProjectDeletionImpactOut(
        key=project.key,
        name=project.name,
        archived=project.archived,
        issues=issues,
        members=members,
        sprints=sprints,
        epics=epics,
        total=members + sprints + epics,
    )


@api.delete("/projects/{key}/")
async def delete_project(request, key: str):
    project = await _visible_project(request, key)
    await _assert_project_admin(request, project)
    await project.adelete()
    return {"deleted": key}


# --- sprint mgmt ---


@api.post("/projects/{key}/sprints/", response=SprintOut)
async def create_sprint(request, key: str, payload: SprintIn):
    """Create a sprint in a project.

    Reuses SprintIn, which the PATCH already accepted. The web UI has had a
    sprint form for a while; this is the same write with no form around it, so
    an agent can plan a sprint without hand-rolling HTTP.
    """
    from ninja.errors import HttpError

    project = await _visible_project(request, key)
    await _assert_project_admin(request, project)
    if not payload.name or not payload.name.strip():
        raise HttpError(400, "name requerido")
    sprint = Sprint(project=project, name=payload.name.strip())
    if payload.goal is not None:
        sprint.goal = payload.goal
    if payload.start_date is not None:
        sprint.start_date = payload.start_date
    if payload.end_date is not None:
        sprint.end_date = payload.end_date
    if payload.retro_notes is not None:
        sprint.retro_notes = payload.retro_notes
    await sprint.asave()
    return sprint


@api.get("/sprints/{sprint_id}/", response=SprintOut)
async def get_sprint(request, sprint_id: int):
    from django.http import Http404

    visible = Project.objects.filter_visible(request.user)
    try:
        return await Sprint.objects.select_related("project").aget(
            pk=sprint_id,
            project__in=visible,
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


@api.delete("/sprints/{sprint_id}/", response=dict)
async def delete_sprint(request, sprint_id: int):
    from django.http import Http404

    try:
        sprint = await Sprint.objects.select_related("project").aget(pk=sprint_id)
    except Sprint.DoesNotExist as exc:
        raise Http404 from exc
    await _assert_project_admin(request, sprint.project)
    await sprint.adelete()
    return {"deleted": sprint_id}


# --- sprint lifecycle ---
#
# `status` is deliberately absent from SprintIn, so PATCH cannot reach it. That
# is not an oversight to be tidied up: the only way to close a sprint is to move
# its unfinished issues somewhere, and a payload with no `carry_to` would do
# exactly half of that — closing the sprint and leaving the issues on a sprint
# that is no longer running. Which is why these are two endpoints rather than a
# writable field.


class SprintStartIn(Schema):
    pass


class SprintCloseIn(Schema):
    #: Sprint to move this sprint's unfinished issues into. Omit to send them
    #: back to the backlog, which is the same thing ``Sprint.aclose`` does with
    #: no argument.
    carry_to: int | None = None


class SprintCloseOut(SprintOut):
    """The closed sprint, plus what the close actually did to the issues.

    Closing a sprint is the one endpoint here that changes rows nobody named in
    the request, so it says so rather than returning a sprint and leaving the
    caller to discover the reassignment later. ``moved_count`` is counted before
    the close, while the issues are still on this sprint.
    """

    moved_count: int = 0
    moved_to: int | None = None


@api.post("/sprints/{sprint_id}/start/", response=SprintOut)
async def start_sprint(request, sprint_id: int, payload: SprintStartIn | None = None):
    """Move a sprint from ``future`` to ``active``.

    A pure state flip, so it is not in the two-step destructive group — nothing
    is lost and it can be undone by closing the sprint again.
    """
    sprint = await _visible_sprint(request, sprint_id)
    await _assert_project_admin(request, sprint.project)
    if sprint.status == "active":
        return sprint
    if sprint.status == "closed":
        from ninja.errors import HttpError

        raise HttpError(400, "un sprint cerrado no se puede arrancar")
    sprint.status = "active"
    sprint.started_at = timezone.now()
    await sprint.asave()
    return sprint


@api.post("/sprints/{sprint_id}/close/", response=SprintCloseOut)
async def close_sprint(request, sprint_id: int, payload: SprintCloseIn):
    """Close a sprint, carrying its unfinished issues to another one or the backlog.

    Answers "how many issues move and where" rather than closing quietly, because
    this is the operation that silently reassigns work: done issues stay put on
    the closed sprint, and everything else goes to ``carry_to`` or nowhere.
    """
    sprint = await _visible_sprint(request, sprint_id)
    await _assert_project_admin(request, sprint.project)
    if sprint.status == "closed":
        return sprint

    from asgiref.sync import sync_to_async

    from projects.models import aclose_sprint_atomic

    # Counted before the close, while the issues are still on this sprint. The
    # alternative — returning whatever the atomic helper happened to move —
    # would say nothing about what the caller was agreeing to.
    unfinished = await Issue.objects.filter(sprint_id=sprint.pk).exclude(status__category="done").acount()
    try:
        closed, _moved = await sync_to_async(aclose_sprint_atomic, thread_sensitive=True)(
            sprint.pk, payload.carry_to
        )
    except ValueError as exc:
        from ninja.errors import HttpError

        raise HttpError(400, str(exc)) from exc
    return SprintCloseOut(
        **SprintOut.from_orm(closed).dict(),
        moved_count=unfinished,
        moved_to=payload.carry_to,
    )


async def _visible_sprint(request, sprint_id: int) -> Sprint:
    """Fetch a sprint the caller can see, or 404.

    The visibility filter is on the *project*, not the sprint, matching
    get_sprint and patch_sprint above. delete_sprint reaches the row by pk and
    lets _assert_project_admin decide instead, which works but is a different
    rule for the same question.
    """
    try:
        sprint = await Sprint.objects.select_related("project").aget(pk=sprint_id)
    except Sprint.DoesNotExist as exc:
        raise Http404 from exc
    visible = Project.objects.filter_visible(request.user)
    if not await visible.filter(pk=sprint.project_id).aexists():
        raise Http404
    return sprint


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
    await _assert_can_edit(request, issue.project)
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
    started = _parse_started(payload.started) if payload.started is not None else None
    _issue, log = await sync_to_async(_log_work_atomic, thread_sensitive=True)(
        issue.pk,
        request.user.pk,
        payload.minutes,
        payload.comment[:255],
        started,
    )
    return WorkLogOut.from_log(log)


class WorkLogPatch(Schema):
    minutes: int | None = None
    comment: str | None = None
    started: str | None = None


@api.patch("/issues/{key}/worklogs/{worklog_id}/", response=WorkLogOut)
async def patch_worklog(request, key: str, worklog_id: int, payload: WorkLogPatch):
    """Correct a logged-time entry: its minutes, comment, or when it happened.

    The late arrival next to add and delete: correcting "3h" to "2h" used to
    mean deleting the entry and recreating it, which lost the original date and
    broke the continuity of the log. The issue's totals move by the delta, under
    the same row lock as the log and unlog paths, so the three agree.
    """
    from asgiref.sync import sync_to_async
    from ninja.errors import HttpError

    from issues.views import _edit_work_atomic

    issue = await _visible_issue(request, key)
    await _assert_can_edit(request, issue.project)
    log = await WorkLog.objects.filter(pk=worklog_id, issue=issue).afirst()
    if log is None:
        raise Http404
    data = payload.dict(exclude_unset=True)
    if "minutes" in data and data["minutes"] is not None and data["minutes"] <= 0:
        raise HttpError(400, "minutes debe ser > 0")
    # None leaves the date alone — a timestamp column has no "cleared" state —
    # while an explicit empty string is malformed input, not absence.
    started = _parse_started(data["started"]) if data.get("started") is not None else None
    # Resolved here rather than inside the helper so the "not on this issue"
    # 404 above stays a 404 about visibility, matching the delete endpoint.
    log = await sync_to_async(_edit_work_atomic, thread_sensitive=True)(
        worklog_id,
        minutes=data.get("minutes"),
        comment=data.get("comment"),
        started=started,
    )
    return WorkLogOut.from_log(log)


@api.delete("/issues/{key}/worklogs/{worklog_id}/", response=WorkLogOut)
async def delete_worklog(request, key: str, worklog_id: int):
    """Remove a logged-time entry and roll its minutes back off the issue.

    Irreversible, so it belongs in the two-step group on the MCP side. Scoped
    through the issue for the same reason comments are: a bare lookup by pk would
    answer 200 for an entry on an issue the caller cannot see.
    """
    from asgiref.sync import sync_to_async

    from issues.views import _unlog_work_atomic

    issue = await _visible_issue(request, key)
    await _assert_can_edit(request, issue.project)
    # Resolved here rather than inside the helper so the "not on this issue" 404
    # is a 404 and not a null dereference deeper in.
    log = await WorkLog.objects.filter(pk=worklog_id, issue=issue).select_related("author", "issue").afirst()
    if log is None:
        raise Http404
    # Serialised before the delete, so there is something to answer with.
    payload = WorkLogOut.from_log(log)
    # The two writes have to be one block, and transaction.atomic() cannot be
    # entered from an async view. Same shape as add_worklog above.
    await sync_to_async(_unlog_work_atomic, thread_sensitive=True)(worklog_id, request.user.pk)
    return payload


@api.delete("/issues/{key}/links/{link_id}/", response=dict)
async def delete_issue_link(request, key: str, link_id: int):
    """Remove a link and the inverse that jirrabit stores alongside it.

    Irreversible. Before this existed a wrongly-created link was permanent
    through the API: it could be added and read but never removed.
    """
    issue = await _visible_issue(request, key)
    await _assert_can_edit(request, issue.project)
    link = await IssueLink.objects.filter(pk=link_id, source=issue).select_related("target").afirst()
    if link is None:
        raise Http404
    # The inverse, not the partner: jirrabit stores both directions, so removing
    # one and leaving its twin behind shows the same relationship twice and
    # makes the remaining half undeletable. Same two statements the UI runs.
    target_key, type_name = link.target.key, link.type
    await IssueLink.objects.filter(
        source_id=link.target_id, target_id=link.pk, type=IssueLink.INVERSE[type_name]
    ).adelete()
    await link.adelete()
    return {"deleted": link_id, "issue": key, "removed_inverse_of": target_key}


# --- search ---------------------------------------------------------------
# Exposes search/jql.py over the API. The parser itself is unchanged and shared
# with the web UI's search page; only the transport is new.

_ARCHIVED_TOKEN = re.compile(r"\barchived\b", re.IGNORECASE)


def _mentions_archived(jql: str) -> bool:
    """Whether a JQL query says anything about ``archived`` itself.

    Used to decide whether to apply the default "hide archived" filter. The
    alternative — always filtering and letting a query opt out some other way —
    would make `archived = true` return nothing at all, since every row would
    have to be both archived and not archived.
    """
    return bool(_ARCHIVED_TOKEN.search(jql or ""))


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
    qs = Issue.objects.filter(condition, project__in=visible)
    # Archived issues are hidden unless the query asks about them, the same
    # default the board and the list endpoint use. A query that names `archived`
    # is left alone, so `archived = true` means "only the archived ones" rather
    # than "archived, and also not archived".
    if not _mentions_archived(jql):
        qs = qs.filter(archived=False)
    qs = (
        qs.select_related(
            "project", "status", "priority", "issue_type", "assignee", "reporter", "parent", "epic"
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


class LinkOut(Schema):
    """A link, with both ends as issue keys rather than numeric ids.

    Breaking change from the first cut of this endpoint, which returned
    ``source``/``target`` as raw ids. A key is the only handle a client has on
    an issue — there is no way to turn 31 back into DEMO-19 — so an id-only link
    is a link the caller cannot act on. The numeric ids stay available as
    ``sourceId``/``targetId`` for anyone who needs them, so this is additive for
    keys and breaking for code reading the old integers off ``source``.
    """

    id: int
    type: str
    source: str
    target: str
    sourceId: int
    targetId: int
    created_by: int
    created_at: datetime


def _link_out(link) -> dict:
    """Shape one IssueLink row into a plain dict.

    Synchronous on purpose: it only reads attributes the caller has already
    prefetched, so there is no query to get wrong. Declaring it async would
    mean every call site had to await it, and a forgotten await hands ninja a
    coroutine instead of a payload.
    """
    return {
        "id": link.pk,
        "type": link.type,
        "source": link.source.key,
        "target": link.target.key,
        "sourceId": link.source_id,
        "targetId": link.target_id,
        "created_by": link.created_by_id,
        "created_at": link.created_at,
    }


@api.get("/link-types/", response=list[str])
async def list_link_types(request):
    """The link types this instance supports, in the model's key spelling."""
    return [choice[0] for choice in IssueLink.TYPE_CHOICES]


@api.get("/issues/{key}/links/", response=list[LinkOut])
async def list_issue_links(request, key: str):
    issue = await _visible_issue(request, key)
    # async for, never a bare unpacking: [*qs] evaluates the queryset, which is
    # a synchronous query and raises SynchronousOnlyOperation from an async
    # view. Collect the pks from both directions, then re-read with both ends
    # joined, because _link_out reads .source.key.
    pks = [pk async for pk in issue.links_out.values_list("pk", flat=True)]
    pks += [pk async for pk in issue.links_in.values_list("pk", flat=True)]
    rows = IssueLink.objects.filter(pk__in=pks).select_related("source", "target")
    return [_link_out(link) async for link in rows]


@api.post("/issues/{key}/links/", response=LinkOut)
async def create_issue_link(request, key: str, payload: IssueLinkIn):
    from ninja.errors import HttpError

    issue = await _visible_issue(request, key)
    await _assert_can_edit(request, issue.project)
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

    link = await IssueLink.objects.acreate(
        source=issue, target=target, type=link_type, created_by=request.user
    )
    return _link_out(link)


# --- watchers -------------------------------------------------------------


@api.get("/issues/{key}/watchers/", response=list[str])
async def list_watchers(request, key: str):
    issue = await _visible_issue(request, key)
    return [u.username async for u in issue.watchers.order_by("username")]


@api.post("/issues/{key}/watchers/", response=list[str])
async def watch_issue(request, key: str):
    """Watch an issue. Idempotent: watching twice is not an error."""
    issue = await _visible_issue(request, key)
    await _assert_can_edit(request, issue.project)
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


class SavedFilterIn(Schema):
    name: str = Field(max_length=120)
    query: str
    scope: str = Field(default="private", max_length=10)


@api.post("/filters/", response=SavedFilterOut)
async def create_saved_filter(request, payload: SavedFilterIn):
    """Store a search under a name.

    The web UI has had both this and the delete for a long time; the API could
    only read, so a client could see the searches its user had saved and could
    not add one or clear one out.
    """
    from ninja.errors import HttpError

    name = payload.name.strip()
    if not name:
        raise HttpError(400, "name requerido")
    if not payload.query.strip():
        raise HttpError(400, "query requerido")
    if payload.scope not in ("private", "shared"):
        raise HttpError(400, "scope inválido; usa 'private' o 'shared'")
    saved = await SavedFilter.objects.acreate(
        owner=request.user, name=name, query=payload.query.strip(), scope=payload.scope
    )
    return saved


@api.delete("/filters/{filter_id}/", response=dict)
async def delete_saved_filter(request, filter_id: int):
    """Delete a saved filter. Owner or superuser, as in the web UI.

    Scoped by owner in the query rather than checked afterwards, so somebody
    else's filter answers 404 instead of 403 — which is the rule every other
    endpoint in this file follows and the one that keeps ids from being
    enumerable.
    """
    qs = SavedFilter.objects.filter(pk=filter_id)
    if not request.user.is_superuser:
        qs = qs.filter(owner=request.user)
    removed, _ = await qs.adelete()
    if not removed:
        raise Http404
    return {"deleted": filter_id}


class StatusTransitionOut(Schema):
    """One reachable status, as the workflow sees it.

    An empty ``allowed_next`` on a status means an open workflow — any
    transition is allowed — so ``open`` is the answer that actually matters and a
    list of names on its own would be a lie in that case.
    """

    id: int
    name: str
    category: str
    open: bool
    next: list[str] = []


@api.get("/statuses/{status_id}/transitions/", response=list[StatusTransitionOut])
async def list_transitions(request, status_id: int):
    """The statuses a work item in this status can legally move to.

    Without this, an agent discovers the workflow by making a call and reading
    the 400 — which is a round trip per transition, and the error only says the
    transition is forbidden, never what would be allowed.
    """
    try:
        status = await Status.objects.prefetch_related("allowed_next").aget(pk=status_id)
    except Status.DoesNotExist as exc:
        raise Http404 from exc
    allowed = [s async for s in status.allowed_next.all()]

    if allowed:
        # A restricted workflow: only the statuses it names are reachable.
        return [
            StatusTransitionOut(
                id=s.pk, name=s.name, category=s.category, open=False, next=[a.name for a in allowed]
            )
            for s in allowed
        ]

    # An empty allowed_next is an open workflow — Status.can_transition_to treats
    # it as "anything goes" — so the honest answer is every status marked open,
    # not an empty list, which would read as "this issue cannot move anywhere".
    every = []
    async for s in Status.objects.order_by("order"):
        every.append(StatusTransitionOut(id=s.pk, name=s.name, category=s.category, open=True, next=[s.name]))
    return every


@api.get("/filters/", response=list[SavedFilterOut])
async def list_saved_filters(request):
    visible = Project.objects.filter_visible(request.user)
    # A filter is visible if it belongs to the caller or was shared, and it must
    # not be able to reach projects the caller cannot see: filters whose JQL
    # references only hidden projects are dropped.
    qs = SavedFilter.objects.filter(models.Q(owner=request.user) | models.Q(scope="shared")).order_by("name")
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
