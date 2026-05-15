FROM python:3.14.4-alpine3.22

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Create a non-root user for the runtime. Devcontainer overrides this via
# ``remoteUser: root`` in devcontainer.json when needed.
RUN addgroup -S app && adduser -S -G app -h /home/app -s /bin/sh app \
    && mkdir -p /opt/venv /app \
    && chown -R app:app /opt/venv /app /home/app

WORKDIR /app

COPY --chown=app:app pyproject.toml uv.lock ./
USER app
RUN uv sync --frozen --no-dev

COPY --chown=app:app . .

USER root
RUN chmod +x /app/docker-entrypoint.sh
USER app

RUN python manage.py collectstatic --noinput || true

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
