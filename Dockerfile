FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

ENV PATH="/app/.venv/bin:$PATH"
ENV DJANGO_SETTINGS_MODULE=jirrabit.settings
ENV JIRRABIT_DEBUG=0

RUN python manage.py collectstatic --noinput || true

EXPOSE 8000

CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "jirrabit.asgi:application"]
