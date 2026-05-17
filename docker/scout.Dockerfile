# =============================================================================
# TrendStorm AI — Scout worker image
# =============================================================================
# The scout worker fetches sources, parses content, and writes RawDocuments.
# It needs the `ingest` group (trafilatura / feedparser / bs4 / defusedxml)
# and the `blob` group (aioboto3 for MinIO uploads).
#
# It does NOT need `agents` (LangGraph) or `llm` (provider SDKs) — those
# live on the orchestrator image.
#
# Build:
#   docker build -f docker/scout.Dockerfile -t trendstorm-scout:latest .
# =============================================================================

# ---- Stage 1: builder ------------------------------------------------------
FROM python:3.12-slim AS builder

ENV UV_VERSION=0.5.11
RUN pip install --no-cache-dir uv==${UV_VERSION}

WORKDIR /build

COPY pyproject.toml uv.lock* ./

ENV UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv venv /app/.venv \
    && uv sync --frozen --no-dev --group ingest --group blob --no-install-project

COPY src/ /build/src/
COPY README.md /build/README.md

RUN uv sync --frozen --no-dev --group ingest --group blob


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

CMD ["python", "-m", "trendstorm.orchestration.workers.scout_worker"]
