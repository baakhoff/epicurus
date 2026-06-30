# syntax=docker/dockerfile:1
# Build from the repo root (the uv workspace):
#   docker build -f services/core-app/Dockerfile -t epicurus-core-app .
FROM python:3.13-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
COPY libs/ ./libs/
COPY services/ ./services/
RUN uv sync --frozen --no-dev --no-editable --package epicurus-core-app

FROM python:3.13-slim-bookworm
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY --from=build /app/.venv /app/.venv
# The container starts as root so the entrypoint can chown the (root-owned, on first boot)
# epicurus-files volume's tenant root — uid 10001 cannot chown a root-owned volume itself. The
# entrypoint then drops to this unprivileged user before exec'ing the app, so the app never runs
# as root. This replaces the files-init one-shot (#421, ADR-0069).
RUN useradd --create-home --uid 10001 epicurus
COPY services/core-app/docker-entrypoint.py /usr/local/bin/docker-entrypoint.py
EXPOSE 8080
ENTRYPOINT ["python", "/usr/local/bin/docker-entrypoint.py"]
CMD ["python", "-m", "epicurus_core_app"]
