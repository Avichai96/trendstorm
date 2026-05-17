# =============================================================================
# TrendStorm AI — SSE Coordinator worker image
# =============================================================================
# Consumes stream.partial.v1 from Kafka, assigns job-scoped seq numbers,
# writes events to Redis Streams (durable log) and Redis Pub/Sub (live fanout).
#
# Only base deps needed — no LLM SDKs, no blob storage, no vector store.
# Smallest worker image in the fleet.
#
# Build:
#   docker build -f docker/sse_coordinator.Dockerfile -t trendstorm-sse-coordinator:latest .
# =============================================================================

# ---- Stage 1: builder ------------------------------------------------------
FROM python:3.12-slim AS builder

ENV UV_VERSION=0.5.11
RUN pip install --no-cache-dir uv==${UV_VERSION}

WORKDIR /build

COPY pyproject.toml uv.lock* ./

ENV UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv venv /app/.venv \
    && uv sync --frozen --no-dev --no-install-project

COPY src/ /build/src/
COPY README.md /build/README.md

RUN uv sync --frozen --no-dev


# ---- Stage 2: runtime ------------------------------------------------------
FROM python:3.12-slim AS runtime

RUN groupadd --system --gid 1000 trendstorm \
 && useradd --system --uid 1000 --gid trendstorm --home /home/trendstorm trendstorm \
 && mkdir -p /home/trendstorm \
 && chown trendstorm:trendstorm /home/trendstorm

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

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "trendstorm.orchestration.workers.sse_coordinator_worker"]
