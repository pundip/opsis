# syntax=docker/dockerfile:1
# Build context is the repository root:
#   docker build -f backend/Dockerfile -t opsis/app .
# One image serves both the api and the worker.

# ---- Stage 1: build the frontend and collect visual manifests -------------
FROM node:24-bookworm-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build \
    && mkdir -p /out/visuals \
    && cd src/visuals \
    && for dir in */; do \
         mkdir -p "/out/visuals/$dir" && cp "${dir}manifest.json" "/out/visuals/$dir"; \
       done

# ---- Stage 2: Python dependencies and application code ---------------------
FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    PATH=/opt/venv/bin:$PATH
COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /usr/local/bin/uv
# pg_dump and pg_restore for backups, at the server's major version (16): Debian's
# own client is 15 and refuses to dump a newer server. From the PostgreSQL apt repository.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
         https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
         > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 opsis \
    && useradd --uid 10001 --gid opsis --create-home opsis \
    && mkdir -p /app /data /backups \
    && chown opsis:opsis /data /backups
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev
COPY backend/alembic.ini ./
COPY backend/opsis ./opsis
COPY spec ./spec
COPY plugins ./plugins
COPY branding.json ./
# The user guide, shown inside the app at {BASE_PATH}/docs/guide.
COPY docs/guide ./guide
COPY --from=frontend /out/visuals ./visuals
COPY --from=frontend /src/frontend/dist ./static
EXPOSE 8080
ENTRYPOINT ["python", "-m", "opsis"]
CMD ["api"]

# ---- Dev and test image: adds pytest, ruff and mypy ------------------------
FROM base AS dev
RUN uv sync --frozen
COPY backend/tests ./tests
USER opsis

# ---- Runtime image (default target) ----------------------------------------
FROM base AS runtime
USER opsis
