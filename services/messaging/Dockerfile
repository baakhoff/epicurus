# syntax=docker/dockerfile:1
# Build from the repo root (the uv workspace):
#   docker build -f services/messaging/Dockerfile -t epicurus-messaging .
FROM python:3.13-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
COPY libs/ ./libs/
COPY services/ ./services/
RUN uv sync --frozen --no-dev --no-editable --package epicurus-messaging

FROM python:3.13-slim-bookworm
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY --from=build /app/.venv /app/.venv
# Run as a non-root user; the service needs no write access at runtime.
RUN useradd --create-home --uid 10001 epicurus
USER epicurus
EXPOSE 8080
CMD ["python", "-m", "epicurus_messaging"]
