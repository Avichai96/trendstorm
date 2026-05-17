"""Lightweight Prometheus /metrics HTTP server for worker processes.

Worker processes (scout, knowledge, analyst, publisher, sse-coordinator,
orchestrator) are not FastAPI apps — they are asyncio Kafka consumers.
They expose /metrics via this minimal aiohttp-free HTTP server built on
asyncio's built-in HTTP server primitives.

Why not aiohttp / FastAPI?
    - Workers already have asyncio; adding another framework would bloat the
      image and introduce a dependency on a heavy HTTP framework.
    - The metrics endpoint only needs one route: GET /metrics.
    - The built-in http.server module is synchronous; we wrap it in
      asyncio.to_thread so it doesn't block the event loop.

Usage (in a worker's run_worker()):
    from trendstorm.infrastructure.metrics.prometheus_server import MetricsServer

    metrics_server = MetricsServer(port=settings.metrics_port)
    await metrics_server.start()
    # ... run worker ...
    await metrics_server.stop()

The server exposes ALL metrics registered via prometheus_client's default
registry (or the registry passed to the constructor).
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, cast
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client import REGISTRY as _DEFAULT_REGISTRY

from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)


class _SilentHandler(WSGIRequestHandler):
    """WSGI handler that suppresses access logs (they're just noise)."""

    def log_message(self, format: str, *args: object) -> None:
        pass


def _metrics_app(registry: CollectorRegistry) -> Any:
    """WSGI app that serves Prometheus metrics."""
    def app(environ: Any, start_response: Any) -> list[bytes]:
        if environ.get("PATH_INFO") == "/metrics":
            output = generate_latest(registry)
            status = "200 OK"
            headers = [("Content-Type", CONTENT_TYPE_LATEST)]
        else:
            output = b"Not Found\n"
            status = "404 Not Found"
            headers = [("Content-Type", "text/plain")]
        start_response(status, headers)
        return [output]
    return app


class MetricsServer:
    """Runs a blocking WSGI server in a background thread.

    The thread is started/stopped via async start()/stop() so workers
    can manage it alongside their asyncio event loop.
    """

    def __init__(
        self,
        port: int = 9090,
        registry: CollectorRegistry | None = None,
    ) -> None:
        self._port = port
        self._registry = registry or _DEFAULT_REGISTRY
        self._server: WSGIServer | None = None
        self._thread: threading.Thread | None = None

    async def start(self) -> None:
        """Start the metrics HTTP server in a background thread."""
        app = _metrics_app(self._registry)
        # make_server is synchronous; run it in a thread pool.
        # cast: asyncio.to_thread returns Any here; we know it's a WSGIServer.
        self._server = cast(
            WSGIServer,
            await asyncio.to_thread(
                make_server,
                "0.0.0.0",  # noqa: S104  # intentional: metrics endpoint is cluster-internal
                self._port,
                app,
                handler_class=_SilentHandler,
            ),
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="metrics-server",
            daemon=True,
        )
        self._thread.start()
        logger.info("metrics_server_started", port=self._port)

    async def stop(self) -> None:
        """Shut down the metrics server."""
        if self._server is not None:
            await asyncio.to_thread(self._server.shutdown)
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        logger.info("metrics_server_stopped")
