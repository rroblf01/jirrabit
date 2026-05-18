FROM python:3.14.4-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    JIRRABIT_STATIC_ROOT=/opt/staticfiles

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Create a non-root user for the runtime. Devcontainer overrides this via
# ``remoteUser: root`` in devcontainer.json when needed.
RUN addgroup -S app && adduser -S -G app -h /home/app -s /bin/sh app \
    && mkdir -p /opt/venv /opt/staticfiles /app \
    && chown -R app:app /opt/venv /opt/staticfiles /app /home/app

WORKDIR /app

COPY --chown=app:app pyproject.toml uv.lock ./
USER app
RUN uv sync --frozen --no-dev

COPY --chown=app:app . .

USER root
RUN chmod +x /app/docker-entrypoint.sh
USER app

# Collect static at build time into /opt/staticfiles (outside /app so a
# bind-mounted source tree in dev does not shadow it). Force DEBUG=1 and a
# throwaway SECRET_KEY so settings.py prod guards do not fire during build.
RUN JIRRABIT_DEBUG=1 JIRRABIT_SECRET_KEY=build-time-only  JIRRABIT_DB_ENGINE=sqlite python manage.py collectstatic --noinput --clear

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["daphne"]
