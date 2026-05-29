# memory_consolidation.Dockerfile — TrendStorm Memory Consolidation Worker
#
# Dep groups: llm + rag (no ingest, no blob, no publish, no eval).
# Needs: aiokafka (consumer/producer), motor (mongo), ChromaDB, LLM providers
#        for semantic extraction (haiku-class) + embedding.
#
# Build (from repo root):
#   docker build -f docker/memory_consolidation.Dockerfile -t trendstorm-memory-consolidation .

# ===========================================================================
# Stage 1: builder — install deps with uv
# ===========================================================================
FROM python:3.12-slim AS builder

WORKDIR /app

RUN pip install --no-cache-dir uv==0.5.20

COPY pyproject.toml uv.lock ./
COPY src/ ./src/

RUN uv sync --frozen --no-dev \
        --group llm \
        --group rag

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
CMD ["python", "-m", "trendstorm.orchestration.workers.memory_consolidation_worker"]
