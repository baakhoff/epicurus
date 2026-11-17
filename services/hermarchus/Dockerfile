# syntax=docker/dockerfile:1
# Hermarchus — the agent host. Build from the repo root (the uv workspace):
#   docker build -f services/hermarchus/Dockerfile -t epicurus-hermarchus .
#
# The image carries the service, git, and the runtimes agent programs are distributed for
# (Python, Node.js + npm). It carries NO vendor agent program: those are installed into the
# instance's home volume (/home/hermarchus) on request and signed in to once, there — see
# docs/services/hermarchus.md.
FROM python:3.13-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
COPY libs/ ./libs/
COPY services/ ./services/
RUN uv sync --frozen --no-dev --no-editable --package epicurus-hermarchus

# Node.js for npm-distributed agent programs (the ACP adapters for Claude Code and Codex).
FROM node:22-bookworm-slim AS node

FROM python:3.13-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl openssh-client procps \
 && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && node --version && npm --version
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 \
    HERMARCHUS_WORKSPACE_DIR=/workspace \
    HERMARCHUS_STATE_DIR=/state \
    HERMARCHUS_AGENT_HOME=/home/hermarchus
# Non-root. The three mount points exist in the image owned by the service user, so a fresh
# named volume mounted over them starts out writable by it (Docker copies the ownership);
# on Kubernetes the chart sets fsGroup to the same id.
RUN useradd --uid 10001 --home-dir /home/hermarchus --create-home --shell /bin/bash hermarchus \
 && mkdir -p /workspace /state \
 && chown 10001:10001 /workspace /state /home/hermarchus
USER hermarchus
WORKDIR /workspace
EXPOSE 8080
CMD ["python", "-m", "epicurus_hermarchus"]
