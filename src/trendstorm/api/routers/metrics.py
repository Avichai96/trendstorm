"""GET /metrics — Prometheus exposition format endpoint for the API service.

Worker processes use MetricsServer (a background thread). The API uses
FastAPI's Response so it integrates with the existing middleware stack.

This endpoint is excluded from:
    - OTel auto-instrumentation (noisy; see shared/tracing/__init__.py)
    - Tenant middleware (no X-Tenant-ID required; Prometheus scraper doesn't send it)
    - Correlation ID middleware (adds irrelevant overhead)
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

router = APIRouter(tags=["observability"])


@router.get(
    "/metrics",
    summary="Prometheus metrics",
    response_class=Response,
    include_in_schema=False,   # hide from Swagger — this is for Prometheus only
)
async def metrics() -> Response:
    """Expose all registered Prometheus metrics in text exposition format.

    Prometheus scrapes this endpoint on the configured scrape interval (15s).
    Do not call this from application code.
    """
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
