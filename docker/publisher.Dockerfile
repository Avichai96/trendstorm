# =============================================================================
# TrendStorm AI — Publisher worker image
# =============================================================================
# Consumes publish.pending.v1: loads Analysis + Category from Mongo, renders
# Markdown / JSON / PDF reports, uploads to MinIO, persists Report docs,
# emits stream events, publishes publish.completed.v1.
#
# Dep groups installed:
#   publish — jinja2, markdown, weasyprint (HTML→PDF)
#   blob    — aioboto3 for MinIO uploads
#
# System packages required by weasyprint (pango/harfbuzz/cairo/gobject):
#   fonts-dejavu  — weasyprint requires at least one system font to render PDFs.
#   libpango-1.0-0, libpangoft2-1.0-0, libcairo2, libharfbuzz0b, libglib2.0-0
#
# Build:
#   docker build -f docker/publisher.Dockerfile -t trendstorm-publisher:latest .
# =============================================================================

# ---- Stage 1: builder ------------------------------------------------------
FROM python:3.12-slim AS builder

ENV UV_VERSION=0.5.11
RUN pip install --no-cache-dir uv==${UV_VERSION}

WORKDIR /build

COPY pyproject.toml uv.lock* ./

ENV UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv venv /app/.venv \
    && uv sync --frozen --no-dev --group publish --group blob --no-install-project

COPY src/ /build/src/
COPY README.md /build/README.md

RUN uv sync --frozen --no-dev --group publish --group blob


# ---- Stage 2: runtime ------------------------------------------------------
FROM python:3.12-slim AS runtime

# System packages required by weasyprint for PDF rendering.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tini \
      fonts-dejavu \
      libpango-1.0-0 \
      libpangoft2-1.0-0 \
      libcairo2 \
      libharfbuzz0b \
      libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 1000 trendstorm \
 && useradd --system --uid 1000 --gid trendstorm --home /home/trendstorm trendstorm \
 && mkdir -p /home/trendstorm \
 && chown trendstorm:trendstorm /home/trendstorm

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
CMD ["python", "-m", "trendstorm.orchestration.workers.publisher_worker"]
