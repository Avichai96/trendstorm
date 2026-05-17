# =============================================================================
# TrendStorm AI — Knowledge worker image
# =============================================================================
# Consumes knowledge.pending.v1: downloads text from MinIO, chunks via pysbd,
# embeds via the configured LLM provider, writes Chunks to Mongo, upserts
# vectors to ChromaDB, publishes knowledge.completed.v1.
#
# Dep groups installed (NOT `agents` — no LangGraph needed here):
#   llm   — embedding SDKs (gemini, openai, ollama)
#   rag   — chromadb, tiktoken, pysbd, onnxruntime
#   blob  — aioboto3 for MinIO downloads
#
# Build:
#   docker build -f docker/knowledge.Dockerfile -t trendstorm-knowledge:latest .
# =============================================================================

# ---- Stage 1: builder ------------------------------------------------------
FROM python:3.12-slim AS builder

ENV UV_VERSION=0.5.11
RUN pip install --no-cache-dir uv==${UV_VERSION}

WORKDIR /build

# Cache-friendly: manifests first.
COPY pyproject.toml uv.lock* ./

# Install deps before copying source so a source-only change doesn't
# bust the layer cache for the heavy llm/rag packages.
ENV UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv venv /app/.venv \
    && uv sync --frozen --no-dev --group llm --group rag --group blob --no-install-project

COPY src/ /build/src/
COPY README.md /build/README.md

RUN uv sync --frozen --no-dev --group llm --group rag --group blob


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
CMD ["python", "-m", "trendstorm.orchestration.workers.knowledge_worker"]
