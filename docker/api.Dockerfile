# =============================================================================
# TrendStorm AI — API service image
# =============================================================================
# Multi-stage build:
#   1. `builder` resolves dependencies with uv into /app/.venv
#   2. `runtime` copies the venv + source into a minimal base image
#
# Build:
#   docker build -f docker/api.Dockerfile -t trendstorm-api:latest .
# Run (against a running stack):
#   docker run --rm -p 8000:8000 --network=trendstorm_default \
#     --env-file .env trendstorm-api:latest
# =============================================================================

# ---- Stage 1: builder ------------------------------------------------------
FROM python:3.12-slim AS builder

# Install uv (Astral's fast resolver) via the official installer.
# Pinning to a uv version means reproducible builds across CI runs.
ENV UV_VERSION=0.5.11
RUN pip install --no-cache-dir uv==${UV_VERSION}

# Workdir for the build.
WORKDIR /build

# Copy ONLY the manifest files first. This is the critical caching trick:
# as long as pyproject.toml + uv.lock don't change, this entire layer is
# cached, and adding/changing source code only invalidates later layers.
COPY pyproject.toml uv.lock* ./

# Create venv at a known path and install runtime deps.
# --no-dev excludes the dev group (pytest, mypy, etc.) — we don't need them
# in the production image.
# --frozen ensures we install exactly what's in uv.lock, no resolver drift.
ENV UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv venv /app/.venv \
    && uv sync --frozen --no-dev --no-install-project

# Now copy the source. This layer invalidates on any code change, but the
# heavy dep-install layer above stays cached.
COPY src/ /build/src/
COPY README.md /build/README.md

# Install the project itself into the venv (editable not required in prod).
RUN uv sync --frozen --no-dev


# ---- Stage 2: runtime ------------------------------------------------------
FROM python:3.12-slim AS runtime

# Run as non-root. A compromised container should not have root inside.
# We create the user explicitly with a stable UID so file ownership is
# predictable across rebuilds (matters for K8s securityContext + volumes).
RUN groupadd --system --gid 1000 trendstorm \
 && useradd --system --uid 1000 --gid trendstorm --home /home/trendstorm trendstorm \
 && mkdir -p /home/trendstorm \
 && chown trendstorm:trendstorm /home/trendstorm

# Minimal runtime dependencies. tini reaps zombie processes (important when
# uvicorn spawns workers); curl is for the HEALTHCHECK below.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini curl \
 && rm -rf /var/lib/apt/lists/*

# Copy the resolved venv + source from builder. Same paths in both stages
# means /app/.venv works as-is.
COPY --from=builder --chown=trendstorm:trendstorm /app/.venv /app/.venv
COPY --from=builder --chown=trendstorm:trendstorm /build/src /app/src
COPY --from=builder --chown=trendstorm:trendstorm /build/pyproject.toml /app/pyproject.toml

# Make the venv the default Python. PYTHONUNBUFFERED is critical for logs:
# without it, stdout is buffered and you don't see log lines until the
# container dies (or stdout fills its buffer).
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app
USER trendstorm

EXPOSE 8000

# Healthcheck used by docker-compose & K8s alike. We hit our own
# liveness endpoint, NOT readiness — readiness probing belongs to the
# orchestrator, not Docker.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/health/live || exit 1

# tini is PID 1 so SIGTERM is properly forwarded to uvicorn, which
# performs a graceful shutdown via our lifespan handler.
ENTRYPOINT ["/usr/bin/tini", "--"]

# uvicorn config:
#   --host 0.0.0.0   bind all interfaces (containerized)
#   --no-access-log  our RequestLoggingMiddleware replaces this
#   --workers 1      one process; horizontal scaling is via K8s replicas
#                    (multiple uvicorn workers complicates shared state)
CMD ["uvicorn", "trendstorm.api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--no-access-log", \
     "--workers", "1"]
