# syntax=docker/dockerfile:1

# ---- build ----------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Resolve dependencies from the lockfile first, in their own layer, so edits to
# application code do not invalidate the (slow) dependency install.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY tfnsw_trip_planner_mcp ./tfnsw_trip_planner_mcp
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- runtime --------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=6401

RUN useradd --create-home --uid 10001 app

WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app tfnsw_trip_planner_mcp ./tfnsw_trip_planner_mcp

USER app
EXPOSE 6401

# Python-only healthcheck; the slim image ships no curl or wget.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['PORT']}/health\").read()"]

CMD ["python", "-m", "tfnsw_trip_planner_mcp"]
