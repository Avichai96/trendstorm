# =============================================================================
# TrendStorm AI — Worker service image
# =============================================================================
# Shares 95% of its definition with api.Dockerfile but installs additional
# dependency groups (agents, llm) needed by the workers.
#
# We deliberately keep a separate Dockerfile rather than parameterizing one
# with a build arg. Two reasons:
#   1. Readability — anyone reading either file sees exactly what's in their
#      image.
#   2. Layer cache — when the API's deps change (e.g. fastapi version bump),
#      we don't want to invalidate the worker's cache, and vice versa.
#
# Build:
#   docker build -f docker/worker.Dockerfile -t trendstorm-worker:latest .
# =============================================================================

# ---- Stage 1: builder ------------------------------------------------------
FROM python:3.12-slim AS builder

ENV UV_VERSION=0.5.11
RUN pip install --no-cache-dir uv==${UV_VERSION}

WORKDIR /build

# Cache-friendly: manifests first.
COPY pyproject.toml uv.lock* ./

# Worker needs `agents` (LangGraph + checkpoint adapter) and `llm` (SDKs).
# We DON'T install `dev` (test infra) or `rag`/`ingest` (separate workers in
# future phases — they get their own images).
ENV UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv venv /app/.venv \
    && uv sync --frozen --no-dev --group agents --group llm --no-install-project

# Now the source.
COPY src/ /build/src/
COPY README.md /build/README.md

RUN uv sync --frozen --no-dev --group agents --group llm


# ---- Stage 2: runtime ------------------------------------------------------
FROM python:3.12-slim AS runtime

RUN groupadd --system --gid 1000 trendstorm \
 && useradd --system --uid 1000 --gid trendstorm --home /home/trendstorm trendstorm \
 && mkdir -p /home/trendstorm \
 && chown trendstorm:trendstorm /home/trendstorm

# Workers don't need curl (no HTTP healthcheck), only tini.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder --chown=trendstorm:trendstorm /app/.venv /app/.venv
COPY --from=builder --chown=trendstorm:trendstorm /build/src /app/src
COPY --from=builder --chown=trendstorm:trendstorm /build/pyproject.toml /app/pyproject.toml

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app
USER trendstorm

# Workers don't expose ports — they're Kafka consumers. They DO have an
# internal-ready signal (the consumer is joined to the group), but that's
# emitted via logs/traces, not via a TCP listener.

# No HEALTHCHECK here. Worker liveness is determined by Kafka group membership
# (a worker that stopped polling gets kicked from the group after
# session_timeout_ms = 60s) and by readable logs. K8s would use exec probes
# checking process existence rather than HTTP.

ENTRYPOINT ["/usr/bin/tini", "--"]

# Module run is the canonical entrypoint — defined in
# orchestration/workers/orchestrator_worker.py:main().
CMD ["python", "-m", "trendstorm.orchestration.workers.orchestrator_worker"]
