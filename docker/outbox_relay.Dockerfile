# outbox_relay.Dockerfile — TrendStorm Outbox Relay Worker
#
# Dep groups: base only (no llm, no rag, no blob, no ingest).
# The relay worker only needs Motor (mongo async) + aiokafka. It is the
# smallest worker in the fleet after sse-coordinator — no LLM SDKs, no blob,
# no vector store. Kafka and Mongo are all it touches.
#
# Build (from repo root):
#   docker build -f docker/outbox_relay.Dockerfile -t trendstorm-outbox-relay .

# ===========================================================================
# Stage 1: builder — install deps with uv
# ===========================================================================
FROM python:3.12-slim AS builder

WORKDIR /app

RUN pip install --no-cache-dir uv==0.5.20

COPY pyproject.toml uv.lock ./
COPY src/ ./src/

# Base dep group only: motor, aiokafka, pydantic, opentelemetry, structlog.
# No --group flags needed: pyproject.toml[project].dependencies covers the base.
RUN uv sync --frozen --no-dev

# ===========================================================================
# Stage 2: runtime — minimal image
# ===========================================================================
FROM python:3.12-slim AS runtime

# tini: PID 1 signal forwarding + zombie reaping
RUN apt-get update && apt-get install -y --no-install-recommends tini && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Non-root user
RUN useradd --create-home --uid 1000 trendstorm
USER trendstorm
WORKDIR /app

COPY --from=builder --chown=trendstorm:trendstorm /app/.venv ./.venv
COPY --from=builder --chown=trendstorm:trendstorm /app/src ./src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "trendstorm.orchestration.workers.outbox_relay_worker"]
