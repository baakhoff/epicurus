# syntax=docker/dockerfile:1
# Build from the repo root (the uv workspace):
#   docker build -f services/tasks/Dockerfile -t epicurus-tasks .
FROM python:3.13-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
# Manifests + workspace sources (lock resolves the whole workspace).
COPY pyproject.toml uv.lock ./
COPY libs/ ./libs/
COPY services/ ./services/
# Install just epicurus-tasks (+ its dependency epicurus-core), non-editable.
RUN uv sync --frozen --no-dev --no-editable --package epicurus-tasks

FROM python:3.13-slim-bookworm
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY --from=build /app/.venv /app/.venv
RUN useradd --create-home --uid 10001 epicurus
USER epicurus
EXPOSE 8080
CMD ["python", "-m", "epicurus_tasks"]
