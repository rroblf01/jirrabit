# jirrabit

A self-hosted issue tracker. Django 6 on Python 3.14, served over ASGI with
WebSockets, server-rendered HTMX frontend, and no frontend build step — htmx and
idiomorph load from a CDN at runtime.

If you want an AI agent to drive it, see [jirrabit-mcp](https://github.com/rroblf01/jirrabit-mcp),
an MCP server that exposes this instance through Atlassian's Jira tool names.

## What it does

Projects with roles, issues with types, statuses, priorities, labels, epics and
sprints; a kanban board and a backlog; comments with threading and reactions;
issue links; attachments; work logging and time tracking; saved filters and a
JQL-lite search; a full activity audit; a superuser-editable workflow; custom
fields; webhooks; project wiki; and live updates over WebSockets.

## Deploying

### Docker (recommended)

```bash
git clone https://github.com/rroblf01/jirrabit
cd jirrabit
docker compose up -d
```

Edit `.env` first if you need to change anything — the compose file reads
exactly `.env`, so renaming it will not be picked up.

Two steps are **not** automatic. The entrypoint's `migrate` subcommand is never
invoked by the compose file, and no seed data is loaded, so a fresh instance
serves an unmigrated database with no statuses or issue types:

```bash
docker compose run --rm web migrate
docker compose exec -T web python manage.py seed_jirrabit
```

Then open <http://localhost:8000> and register an account. The shipped `.env`
has `JIRRABIT_DEBUG=1`, so registration is open.

If you change `.env`, use `docker compose up -d` to recreate the containers.
`docker compose restart` keeps the old environment, because Compose reads
`env_file` at container creation.

### Without Docker

```bash
uv sync --frozen
docker compose run --rm web migrate          # or export the DB vars and use manage.py
```

`set -a; . ./.env; set +a` is the reliable way to load them, since
`JIRRABIT_SECRET_KEY` contains characters that break naive `xargs`-based
one-liners. With the database reachable:

```bash
uv run python manage.py migrate
uv run python manage.py seed_jirrabit
uv run saltare jirrabit.asgi:application
```

Use `saltare`, not `runserver`: the server is ASGI and Channels, and
`runserver` silently drops the WebSocket connections.

## Configuration

All configuration is environment variables, read in `jirrabit/settings.py`.
There is no dotenv loader: `.env` is consumed only by `docker-compose.yml`.

| Variable | Default | Notes |
|---|---|---|
| `JIRRABIT_SECRET_KEY` | dev fallback | **Required** when `JIRRABIT_DEBUG=0` |
| `JIRRABIT_DEBUG` | `1` | `0` forces secure cookies, HSTS, SSL redirect, invite-only registration |
| `JIRRABIT_ALLOWED_HOSTS` | `jirrabit.ricardorobles.es,localhost,127.0.0.1` | Comma-separated. An unlisted `Host` gets a 400 |
| `JIRRABIT_DATABASE_URI` | — | `postgres://…`. **Required** unless `JIRRABIT_DB_ENGINE=sqlite` |
| `JIRRABIT_DB_ENGINE` | `postgres` | `sqlite` for local work and tests |
| `REDIS_URL` | — | Absent means the in-memory channel layer, so no WebSockets across processes |
| `JIRRABIT_INVITE_ONLY` | `1` when DEBUG is off | Set `0` for an open instance |
| `JIRRABIT_EMAIL_BACKEND` | console | Use `smtp` with the `JIRRABIT_EMAIL_*` variables |
| `JIRRABIT_API_RATE_LIMIT` | `0` (off) | Requests per window, per API key. `0` disables throttling |
| `JIRRABIT_LANGUAGE` / `JIRRABIT_TIMEZONE` | `es-es` / `Europe/Madrid` | |
| `JIRRABIT_STATIC_ROOT` | `staticfiles/` | |

Two traps worth knowing:

- **`POSTGRES_*` in `.env` configure the postgres container, not Django.**
  Django reads only `JIRRABIT_DATABASE_URI` and raises at startup if it is empty
  or its scheme is not `postgres://`. Change a password in both places.
- **Behind TLS, set your own domain in `JIRRABIT_ALLOWED_HOSTS`** or every
  request answers 400. If a containerised jirrabit-mcp will reach the instance
  as `host.docker.internal`, include that name too.

## Development

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format .
JIRRABIT_DB_ENGINE=sqlite uv run python manage.py test tests
```

The `JIRRABIT_DB_ENGINE=sqlite` prefix is required for any host-side
`manage.py` call, or Django tries to reach a Postgres that is not there.

## Management commands

| Command | Purpose |
|---|---|
| `seed_jirrabit` | Create the default statuses, priorities and issue types. Idempotent |
| `seed_demo` | Build a demo project. **Wipes the database** unless `--no-clear` |
| `auto_archive --days N` | Archive done issues resolved more than N days ago. For a cron job |
| `purge_old_data --days N` | Delete old audit entries and read notifications. For a cron job |
| `import_jira data.csv --project KEY --reporter USER` | Import a Jira CSV export |

`auto_archive` and `purge_old_data` both take `--dry-run`.

## The API

`/api/v1/`, with interactive docs at `/api/v1/docs` and an OpenAPI schema at
`/api/v1/openapi.json`. Authenticate with `Authorization: Bearer <APIKey>`, where
the key comes from your profile's API keys page.

`createJiraProject` and `searchJiraIssuesUsingJql` are not exposed yet; the rest
of what an agent needs is reachable. See `AGENTS.md` for the architecture.

## License

MIT.
