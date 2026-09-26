# jirrabit

Self-hosted issue tracker: Django 6 on Python 3.14, served over ASGI with
WebSockets, server-rendered HTMX frontend, no frontend build step.

## Commands

Run from the repo root. `uv` manages the venv; do not create one by hand.

```bash
uv sync --frozen                                    # deps; uv.lock is authoritative

uv run ruff check .                                # lint (CI-blocking)
uv run ruff format .                               # formatter, line-length 110
uv run ty check .                                  # pre-release; CI ignores failures

JIRRABIT_DB_ENGINE=sqlite uv run python manage.py test tests
JIRRABIT_DB_ENGINE=sqlite uv run python manage.py test tests.test_smoke.PermissionTests
JIRRABIT_DB_ENGINE=sqlite uv run python manage.py check
JIRRABIT_DB_ENGINE=sqlite uv run python manage.py makemigrations --check --dry-run
```

**The `JIRRABIT_DB_ENGINE=sqlite` prefix is not optional** for any host-side
`manage.py` call. The default engine is Postgres, and a bare
`manage.py test` on a laptop fails trying to reach a database that is not there.
CI sets the same variable (`.github/workflows/ci.yml`).

### Docker

```bash
docker compose up -d                      # web on :8000, postgres, redis
docker compose run --rm web migrate       # REQUIRED on a fresh volume
docker compose exec -T web python manage.py seed_jirrabit
docker compose logs -f web
```

Migrations are **not** run automatically. `docker-compose.yml` overrides the
command with `saltare-dev` and the entrypoint's `migrate` subcommand is never
invoked, so a fresh `docker compose up` serves an unmigrated database. Run
`migrate` yourself, then `seed_jirrabit` — without it there are no `Status`,
`Priority` or `IssueType` rows and issue creation fails on a null lookup.

The postgres volume mounts at `/var/lib/postgresql`, **not**
`/var/lib/postgresql/data`. Postgres 18 images store data under a
major-version subdirectory and refuse to start if a volume lands on the old
path, so moving that mount back breaks the database with a confusing error
about "PostgreSQL data in the wrong place".

`env_file` values are read when a container is **created**. After editing `.env`,
use `docker compose up -d`; `docker compose restart` keeps the old environment.

## Configuration

All environment variables, read in `jirrabit/settings.py`. There is no dotenv
loader: `.env` is consumed only by `docker-compose.yml`, so a host-side
`manage.py` invocation needs the variables exported in your shell.

Variables that look settable but are not:

- **`POSTGRES_*`** are read by the **postgres container**, not by Django. Django
  reads only `JIRRABIT_DATABASE_URI`, and raises if it is empty or its scheme is
  not `postgres://`. The two overlap confusingly; changing the password means
  changing it in both places.
- **`JIRRABIT_API_RATE_LIMIT`** defaults to `0`, which means *no* throttling. A
  non-positive limit is an early return in `core/middleware.py`; the check below
  it is `len(attempts) >= limit`, so a limit of `0` without that guard would
  throttle every request.

Modes:

- `JIRRABIT_DEBUG=1` (default): SQLite-free Postgres, console email, in-memory
  channels, non-secure cookies.
- `JIRRABIT_DEBUG=0`: hard-fails without `JIRRABIT_SECRET_KEY`, forces secure
  cookies, HSTS and SSL redirect, and **turns on invite-only registration**
  (`JIRRABIT_INVITE_ONLY` defaults to `1` when DEBUG is off). For a demo or a toy
  instance, set `JIRRABIT_INVITE_ONLY=0` explicitly.
- `JIRRABIT_ALLOWED_HOSTS` is a comma-separated list and is genuinely read. An
  unlisted `Host` gets a 400. Add `host.docker.internal` if a containerised
  jirrabit-mcp will reach this instance by that name.

## Architecture

Django apps, each owning one slice:

| App | Owns |
|---|---|
| `jirrabit/` | settings, urls, and `api.py` — the whole REST API |
| `core/` | No models. Middleware, async view bases, permission helpers, markdown, palettes, audit, webhooks, notifications, `purge_old_data` |
| `accounts/` | `User` (the `AUTH_USER_MODEL`), `APIKey`, `InviteToken`, `Notification`, `Team`, `DashboardWidget` |
| `projects/` | `Project`, `ProjectMembership`, `Epic`, `Sprint`, `SavedFilter`, `Webhook`, `CustomFieldDef`, `ProjectWiki` |
| `issues/` | `Issue`, `Status`, `IssueType`, `Priority`, `Label`, `Comment`, `IssueLink`, `WorkLog`, `Attachment`, `HistoryEntry`, `AuditEntry`, and the superuser-only workflow editor |
| `board/` | `SavedBoardView` only — the board itself is a virtual projection over issues |
| `search/` | JQL-lite parser (`jql.py`) and typeahead. No models |
| `realtime/` | WebSocket consumers and routing. No models |

### Everything is async

Served by `saltare` (ASGI), not `runserver`, which would drop WebSockets.

- Views are `async def`. Django's generic CBVs do not work; use the bases in
  `core/async_views.py`, which keep the familiar surface but take `aget_*` /
  `aform_*` hooks. `jirrabit/api.py` follows the same rule: all 31 endpoints and
  all its helpers are `async def`.
- `core/aio.py` holds the **only** sanctioned `sync_to_async` shims: `arender`,
  `avalid`, `asave_m2m`, `aform`. Everything else must use `aget`/`asave`/`aset`/
  `acreate`/`afirst`.
- Context processors run before the view and cannot touch the database, so
  middleware pre-loads what they need onto `request` (see
  `core/context_processors.py` and `nav_context_middleware`).

Four places cannot be async, and forcing them is a bug rather than a style
question:

- **`transaction.atomic()`** raises `SynchronousOnlyOperation` from an async
  view. A transaction has to be one unbroken block, so the whole unit goes in a
  sync helper called through `sync_to_async(..., thread_sensitive=True)`.
  `issues.views._change_status_atomic` and `_log_work_atomic` are both shaped this
  way, and the API uses them rather than re-implementing the locking.
- **Management commands** have no async support at all: Django 6's `BaseCommand`
  has neither `iscoroutinefunction` nor `async_to_sync`, so `execute()` writes
  the return value to stdout and an `async def handle` hands it a coroutine.
  A command's ORM calls therefore have to be the sync ones.
- **`APIKeyAuth.authenticate`** stays sync because django-ninja calls it sync.
- **Model methods that read a relation** — `Status.can_transition_to` calls
  `.allowed_next.all()`. Callers on the event loop must
  `prefetch_related("status__allowed_next")` first, or that becomes a synchronous
  query. `api.py` prefetches it in the issue querysets for this reason.

Note that `QuerySet` has **no** `avalues_list` and no `aall`: fetch the rows and
read the attribute in Python, or iterate with `async for`.

### Saving an issue does more than it looks

Four independent `post_save` receivers are wired in `core/apps.py`:
notifications, audit, webhooks and the realtime broadcast. So:

> `aupdate()`, `bulk_create()` and `QuerySet.update()` silently skip
> notifications, audit rows, webhooks **and** WebSocket pushes.

Use `asave()`. `accounts.signals` also maintains `User.unread_count` from
`post_save`, so a bulk update leaves the badge wrong.

This trap is **worse in async code**, which is why it is called out separately:
`aupdate()` is the obvious async spelling of `update()`, so reaching for it feels
like progress when it is actually a silent regression. Two paths had exactly
this bug and both are now covered by `tests/test_bulk_updates.py`:

- `board.BulkUpdateView` set `status_id` with `aupdate()`, which skipped the
  workflow check, `HistoryEntry` and all four receivers.
- `Sprint.aclose` moved issues between sprints with `aupdate(sprint_id=...)`,
  so watchers were never notified and the board went stale.

The general rule: in a request path, prefer a loop of `asave()` over any bulk
write. If a bulk write is genuinely right, it must be one of the two exceptions
documented under **Workflow**, and it must say in a comment why.

### Board ordering

`Issue.rank` is the card's 0-based index inside its `(project, status)` column,
and the invariant is that a column is always a dense `0..n-1` run. Three things
maintain it, and a new path that moves a card between columns has to call one of
them or the board grows a hole:

- `board.views._apply_board_order` renumbers a column to a caller-supplied
  order. `ReorderView` uses it, taking the whole column as the browser sees it
  rather than `before`/`after` neighbours, which is idempotent and has no
  midpoint arithmetic.
- `board.views._append_to_column` puts a card at the end of its new column.
  Every status change that carries no position information goes through it: the
  move endpoint, the advance button, the bulk status action.
- `Issue.save()` appends a new card, on insert only.

`static/js/board.js` reads the drop point from the DOM and sends the resulting
column order. The board orders by `("rank", "-updated_at")` and then groups by
status in Python, which works because the grouping is stable. **The backlog
deliberately does not order by `rank`** — it is a board-column concept and means
nothing in a flat list.

A card's status change and its re-rank are two saves, so one drag writes two
`AuditEntry` rows. That is honest rather than tidy: both describe a field that
really changed.

### Workflow

`Status.allowed_next` is a self-M2M. **An empty list means an open workflow** —
any transition is allowed. `Status.can_transition_to()` is the check.

Every transition goes through `issues.views._change_status_atomic()`, which
validates, takes `select_for_update`, sets or clears `resolved_at` by
`category == "done"`, and writes a `HistoryEntry`. `ChangeStatusView`,
`AdvanceStatusView` and `board.MoveCardView` all delegate to it.

Both write paths now go through it, including the two that used to bypass it:

- `jirrabit/api.py`'s `patch_issue` applies `status_id` separately from the rest
  of the PATCH, through `sync_to_async(_change_status_atomic)`, and re-reads the
  issue afterwards because the in-memory copy predates the transition.
- `board.BulkUpdateView` loops the chokepoint over the selected issues, so the
  board cannot reach a status the workflow forbids.
- `board.ReorderView` also routes a card whose status changed through it, so a
  drag between columns is validated exactly like a drag inside one.

Remaining gaps, verified:

- `HistoryEntry` is written by only two code paths, so any "changelog" built from
  it is incomplete by construction. `AuditEntry` is the separate, signal-driven
  history and has **no actor**.
- **`core/webhooks.py`'s `auto_assign_lead`** uses a raw `aupdate()` on purpose.
  It runs *from* a webhook that fired off `Issue.post_save`, so `asave()` would
  re-enter the dispatch loop forever. It therefore leaves no audit row.
- **`Project.anext_issue_number`** also uses `aupdate()`, to bump a counter
  without firing the `Project` receivers on every issue creation.

`aupdate()` and `adelete()` are otherwise banned in request paths. If a bulk
write has to reach several issues, use `board/views.py::_asave_each`, which
saves row by row and says why.

### Permissions

- Roles are `admin` / `member` / `viewer` on `ProjectMembership`. Superuser and
  the project **lead** both resolve to `admin` (`core/permissions.py`).
- `Project.objects.filter_visible(user)` is the single visibility gate. Use it in
  every queryset — the API, search, nav and board all go through it.

  It used to be skipped by the search results page, which meant `project = OPS`
  in the search box listed every issue in a project the caller was not a member
  of. A JQL clause names the project to read, so a query that trusts it is a
  data leak; `tests/test_interactive_flows.py` now pins the scoping.
- Assertions are explicit inside async view bodies:
  `await aassert_can_edit(request.user, issue.project)`.
- The REST API re-implements these checks locally instead of calling
  `core.permissions`, and `accounts/admin_views.py` defines its own
  `AsyncSuperuserRequiredMixin`. Three copies exist; prefer `core.permissions`
  for new code.

## REST API

`jirrabit/api.py`, django-ninja, mounted at `/api/v1/`. Interactive docs at
`/api/v1/docs`, OpenAPI at `/api/v1/openapi.json`.

- Auth is **session cookie or `Authorization: Bearer <APIKey>`**. The two auth
  handlers are ordered `[APIKeyAuth(), django_auth]` and that order is load
  bearing: `django_auth` subclasses `APIKeyCookie`, which enforces CSRF on unsafe
  methods, and listing it first rejected every Bearer write with
  "CSRF check Failed". Reversing it fixes Bearer clients and keeps CSRF for
  cookie sessions.
- Pagination is a hand-rolled envelope: `count` / `page` / `size` / `pages` /
  `next` / `previous` / `items`. Not Jira's `startAt` + `isLast`.
- Errors are always `{"detail": "..."}`, including the rate-limit 429, which also
  carries `Retry-After`.
- Write endpoints take numeric `*_id` fields. The read-only metadata endpoints
  (`/issue-types/`, `/statuses/`, `/priorities/`, `/users/search/`) exist so a
  client can resolve a name to an id.

## Templates and frontend

- Templates live in the project-level `templates/` only. No app has a
  `templates/` dir, despite `APP_DIRS = True`.
- `base.html` sets `hx-boost="true"`, `hx-ext="morph"` and
  `hx-swap="morph:innerHTML"` on `<body>`, plus `hx-headers` for CSRF. Partials
  are `_`-prefixed and must be self-contained.
- Returning partial vs. full page is a recurring idiom:
  `if request.htmx and not request.headers.get("HX-Boosted"): return [".../_partial.html"]`.
- **No frontend build.** No npm, no bundler, no Tailwind. htmx 2.0.10 and
  idiomorph 0.7.4 load from unpkg at runtime; CSP allows `unpkg.com` plus
  `unsafe-inline` and `unsafe-eval`, which idiomorph requires.
- One stylesheet, `static/css/jirrabit.css`, themed through `--blue-*`,
  `--ink-*` and `--surface*` custom properties. `core/palettes.py` overrides
  those same variables per user palette, so add new colours as variables.
- `static/js/ux.js` is the bulk of the behaviour, namespaced under
  `window.jirrabit`.

## Data quirks

- `Attachment` stores the file **base64-encoded in a `TextField`**, capped at
  5 MB. That is why CSP needs `frame-src data:`.
- `Issue.key` is generated in `Issue.save()` from `project.next_issue_number()`,
  not by a form or the API.
- `Issue.description_html` and `Comment.body_html` are render-on-save caches with
  a lazy fallback.
- `Comment.is_internal` is force-cleared for non-staff on every submit.
- `Issue.rank` is the card's index inside its `(project, status)` board column,
  and it is written. It used to be declared, read by the board and never written,
  which is why the kanban silently fell back to `updated_at` — see
  **Board ordering** below.
- `core/worker.enqueue()` is an in-process `asyncio.Queue`, not a broker. Tasks
  are lost on restart.
- `core/webhooks.py` dispatches to in-process stub actions that mostly log. There
  is no outbound HTTP.

## i18n

The UI is Spanish-first: `LANGUAGE_CODE = "es-es"`, `LANGUAGES = [es, en]`.
User-facing strings, API error messages and `PermissionDenied` texts are
Spanish; code, comments and docstrings are English. `locale/*/LC_MESSAGES/*.po`
and `.mo` are committed. The image has no `gettext`, so `compilemessages` needs
it installed locally.

## Tests

`tests/test_smoke.py` is the only suite: Django `TestCase`, each test builds its
own data, no shared fixtures, no pytest. Add to it or create a sibling module and
run it with `manage.py test <module>`.

## MCP

A separate project, [jirrabit-mcp](https://github.com/rroblf01/jirrabit-mcp),
exposes this instance over MCP using Atlassian's Jira tool names. It is a plain
HTTP client of `/api/v1/`, so anything it needs is an endpoint here. When a tool
is missing, the gap is almost always a missing endpoint, not a missing tool
implementation.

It is multi-tenant: each tool call carries its own `instanceUrl` and `apiKey`, so
one deployed server serves many jirrabit instances. That means an API key is
normally created per person, and `host.docker.internal` must be in
`JIRRABIT_ALLOWED_HOSTS` for a containerised deployment to get past Django's Host
check.

Run its checks against a running instance:

```bash
cd ../jirrabit-mcp
JIRRABIT_URL=… JIRRABIT_API_KEY=… go run ./cmd/smoke -server ./bin/jirrabit-mcp
```

## Cron jobs

`auto_archive` and `purge_old_data` are designed for a scheduler, not manual
runs. Both take `--dry-run`; `seed_demo` **wipes the database** unless passed
`--no-clear`.
