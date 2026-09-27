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

That does not fetch the MCP submodule, which the compose file only needs when
you ask for the `mcp` profile. To get everything:

```bash
git clone --recurse-submodules https://github.com/rroblf01/jirrabit
```

or, in a clone you already have:

```bash
git submodule update --init
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
| `JIRRABIT_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated. An unlisted `Host` gets a 400, so set your own domain here |
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

## The MCP server

[jirrabit-mcp](https://github.com/rroblf01/jirrabit-mcp) exposes this instance
over MCP using Atlassian's Jira tool names, so an agent that already knows Jira
needs no new vocabulary. It is a submodule of this repository, pinned to a
commit.

```bash
git submodule update --init          # once, after cloning
docker compose --profile mcp up -d   # both services, one network
```

The MCP service is behind a profile so that a plain `docker compose up -d` stays
a fast Python-only loop and does not pay for a Go build.

### Using it

The server holds no credentials. Every tool call carries its own `instanceUrl`
and `apiKey`, so one deployment serves many jirrabit instances and one person's
key is never sent to another person's data.

To try it: register at <http://localhost:8000>, then create an API key under
your profile (**API keys**). The plaintext is shown once — only its SHA-256 is
stored. Then point an MCP client at it.

#### The public demo token

To try the MCP without registering, set one fixed, published API key for
`alice_pm` and every `seed_demo` will hand it out again:

```bash
# in .env
JIRRABIT_DEMO_API_KEY=jirrabit-public-demo-token-2026-do-not-use
```

That is the whole setup. There is no second command to remember, because
`seed_demo` truncates every table on each run — API keys included — so a token
minted by hand stops existing at the next daily seed, and the failure is silent:
the instance is up and the token simply stops authenticating. So the seed
re-mints it at the end, when the variable is set, and the demo token keeps
working across reseeds. `seed_demo_api_key` still exists and can be run on its
own, but on a daily `seed_demo` you do not need it.

The token is then usable as:

```json
{ "instanceUrl": "https://jirrabit.ricardorobles.es",
  "apiKey": "jirrabit-public-demo-token-2026-do-not-use" }
```

Both are opt-in and idempotent: with the variable unset no key is created at all,
and with it set the same token always lands on the same key rather than piling up
duplicates. Unset means `seed_demo` still refuses to invent a token of its own —
a predictable credential is a published credential, and that is the operator's
decision to make.

#### This repository is public, so read this before using that token

**The token above is printed in a public README. Anyone on the internet can read
it.** The only thing standing between that token and your data is whether your
instance is reachable and whether you seeded it. Specifically:

- On a laptop, or any instance behind a firewall or a VPN, the token is
  unreachable and harmless. That is the case it is meant for.
- On an instance with a public hostname — and this repository ships
  `jirrabit.ricardorobles.es` in its own `.env` — anyone who reads this README can
  authenticate as `alice_pm` and do everything that user can do, including
  reading every issue, every comment and every attachment.

So: never run `seed_demo_api_key` against an instance you care about, and never
reuse the token. If you want the convenience on a shared instance, generate a
different token and keep it out of version control:

```bash
docker compose exec -T -e JIRRABIT_DEMO_API_KEY="$(openssl rand -hex 24)" \
  web python manage.py seed_demo_api_key
```

The same applies to `.env`, which is committed to this public repository. Its
`JIRRABIT_SECRET_KEY` and `POSTGRES_PASSWORD` are development placeholders
(`django-insecure-…`, `jirrabit-local-dev`) and are in the git history
permanently. Treat both files as public, and set fresh values in the
environment of anything you deploy.

With the MCP in Docker, the instance is reachable by its compose service name.
A stdio client launches the binary through `docker exec`, and the transport has
to be forced because the image sets `JIRRABIT_MCP_TRANSPORT=http`:

```json
{
  "mcp": {
    "jirrabit": {
      "type": "local",
      "command": [
        "docker", "exec", "-i",
        "-e", "JIRRABIT_MCP_TRANSPORT=stdio",
        "jirrabit-jirrabit-mcp-1", "/usr/local/bin/jirrabit-mcp"
      ],
      "enabled": true
    }
  }
}
```

and pass `"instanceUrl": "http://web:8000"` with the key on each call. An HTTP
client can connect to <http://localhost:8082/mcp> instead, with no
configuration at all beyond the URL.

`web` is in the default `JIRRABIT_ALLOWED_HOSTS` for exactly this reason: Django
answers 400 for an unlisted `Host`, and inside the compose network the MCP's
requests arrive as `Host: web:8000`.

### Updating the submodule

The pointer is pinned to a commit, which is the point: the compose always builds
the version this repository recorded, so MCP work cannot reach your stack until
you bump it deliberately.

```bash
git submodule update --remote jirrabit-mcp
git add jirrabit-mcp && git commit -m "chore: bump jirrabit-mcp"
docker compose --profile mcp up -d --build
```

The last step is not optional. A bumped pointer does not rebuild a running
container, and the missing `--build` would reuse the previous image.

If you develop the MCP in a separate checkout, set `MCP_CONTEXT` in `.env` to
that path and the compose builds from it instead.

## Development

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format .
JIRRABIT_DB_ENGINE=sqlite uv run python manage.py test tests
```

The `JIRRABIT_DB_ENGINE=sqlite` prefix is required for any host-side
`manage.py` call, or Django tries to reach a Postgres that is not there.

## Backup and restore

Everything lives in Postgres. Attachments are base64 in a `TextField` rather
than files on disk, so there is no second thing to back up and no path to keep
in sync — a `pg_dump` of the database is a complete backup of the instance.

```bash
# Back up. Plain SQL by default: inspectable, and it restores anywhere.
docker compose exec -T jirrabit-db pg_dump -U jirrabit jirrabit > jirrabit-$(date +%F).sql

# Faster, but binary and only restorable into a compatible Postgres.
docker compose exec -T jirrabit-db pg_dump -U jirrabit -Fc jirrabit > jirrabit-$(date +%F).dump

# Restore. DESTRUCTIVE: it replaces the current contents.
docker compose exec -T jirrabit-db psql -U jirrabit -c 'SELECT 1' >/dev/null
docker compose exec -T jirrabit-db dropdb -U jirrabit jirrabit
docker compose exec -T jirrabit-db createdb -U jirrabit jirrabit
docker compose exec -T -i jirrabit-db psql -U jirrabit -d jirrabit < jirrabit-2026-01-01.sql
```

Restore into a **stopped** `web`, or the running process will keep writing to a
database that is being replaced underneath it. Migrations are not re-run by a
restore: the dump carries `django_migrations`, so the schema and the recorded
migrations agree by construction.

A nightly cron is the whole story for a self-hosted instance:

```cron
17 3 * * * cd /srv/jirrabit && docker compose exec -T jirrabit-db pg_dump -U jirrabit jirrabit | gzip > backups/jirrabit-$(date +%F).sql.gz
```

A backup you have not restored from is a hypothesis. Once a quarter, restore one
into a scratch database and check the issue count.

## Management commands

| Command | Purpose |
|---|---|
| `seed_jirrabit` | Create the default statuses, priorities and issue types. Idempotent |
| `seed_demo` | Build a demo project. **Wipes the database** unless `--no-clear` |
| `seed_demo_api_key` | Give `alice_pm` a fixed API key from `JIRRABIT_DEMO_API_KEY`. `seed_demo` already does this when the variable is set; run it alone to mint the key without reseeding |
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
