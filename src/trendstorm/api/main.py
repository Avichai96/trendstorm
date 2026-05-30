"""FastAPI application factory and lifespan management.

Boot sequence (lifespan startup):
    1. Configure logging — first, so subsequent startup logs are structured.
    2. Configure tracing — second, so spans are emitted from boot onwards.
    3. Construct infrastructure clients with typed settings.
    4. Connect each client in parallel (fail fast on any failure).
    5. Attach clients to app.state for DI providers.
    6. (Future: start background tasks like Kafka consumer for SSE coordinator.)

Shutdown sequence (lifespan teardown — reverse order):
    1. Stop background tasks (drain).
    2. Stop Kafka producer (flush pending messages).
    3. Close Redis client.
    4. Close Mongo client.
    5. Flush OTel spans.

Why parallel startup connects?
    - Cuts cold-start time roughly in half.
    - Each client has its own timeout; we don't serialize them.
    - On failure, we still get useful logs from each (gather with
      return_exceptions=True would let one failure mask others; we use
      gather + raise instead, accepting that the first failure aborts).

Run:
    uvicorn trendstorm.api.main:app --host 0.0.0.0 --port 8080

For production:
    uvicorn trendstorm.api.main:app --workers 4 --no-access-log
    (Our own RequestLoggingMiddleware replaces uvicorn's access log.)
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from trendstorm import __version__
from trendstorm.api.error_handlers import install_exception_handlers
from trendstorm.api.middleware.auth import AuthMiddleware
from trendstorm.api.middleware.correlation import CorrelationIdMiddleware
from trendstorm.api.middleware.rate_limit import RateLimitMiddleware
from trendstorm.api.middleware.request_logging import RequestLoggingMiddleware
from trendstorm.api.middleware.tenant import TenantMiddleware
from trendstorm.api.routers import api_keys as api_keys_router
from trendstorm.api.routers import audit as audit_router
from trendstorm.api.routers import auth as auth_router
from trendstorm.api.routers import categories as categories_router
from trendstorm.api.routers import health as health_router
from trendstorm.api.routers import invites as invites_router
from trendstorm.api.routers import jobs as jobs_router
from trendstorm.api.routers import memberships as memberships_router
from trendstorm.api.routers import memories as memories_router
from trendstorm.api.routers import metrics as metrics_router
from trendstorm.api.routers import organizations as organizations_router
from trendstorm.api.routers import quota as quota_router
from trendstorm.api.routers import reviews as reviews_router
from trendstorm.api.routers import sources as sources_router
from trendstorm.api.routers import users as users_router
from trendstorm.infrastructure.blob.minio_client import MinioClient
from trendstorm.infrastructure.kafka.producer import KafkaProducerClient
from trendstorm.infrastructure.llm.registry import build_embedding_provider
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.redis.client import RedisClient
from trendstorm.infrastructure.redis.pubsub import RedisPubSub
from trendstorm.infrastructure.redis.streams import RedisStreamStore
from trendstorm.infrastructure.vectors.chroma_store import ChromaVectorStore
from trendstorm.shared.config import Settings, get_settings
from trendstorm.shared.logging import configure_logging, get_logger
from trendstorm.shared.tracing import (
    configure_tracing,
    instrument_fastapi,
    shutdown_tracing,
)

logger = get_logger(__name__)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: startup before yield, shutdown after.

    Anything attached to `app.state` here is available to DI providers
    via `request.app.state`.
    """
    settings: Settings = app.state.settings

    # ---- Startup --------------------------------------------------------
    logger.info(
        "app_starting",
        version=__version__,
        env=settings.app.env.value,
        log_format=settings.app.log_format.value,
    )

    # Instantiate clients (no I/O yet)
    mongo = MongoClient(settings.mongo)
    redis = RedisClient(settings.redis)
    kafka_producer = KafkaProducerClient(settings.kafka)
    blob = MinioClient(settings.blob)
    vector_store = ChromaVectorStore(settings.vector)

    # Connect in parallel — fail fast if any blow up.
    try:
        await asyncio.gather(
            mongo.connect(),
            redis.connect(),
            kafka_producer.start(),
            blob.connect(),
            vector_store.connect(),
        )
    except Exception:
        logger.exception("startup_failed")
        # Attempt cleanup of whatever did connect
        await _safe_shutdown(mongo, redis, kafka_producer, blob, vector_store)
        raise

    # Embedding provider: construction only (no network call). Falls back to
    # None if the configured provider's API key is missing or invalid so that
    # non-vector endpoints can still serve.
    try:
        embedding_provider = build_embedding_provider(settings)
    except Exception as exc:
        logger.warning("embedding_provider_config_error", error=str(exc))
        embedding_provider = None

    # SSE infrastructure — wraps the same Redis client; no extra connections.
    stream_store = RedisStreamStore(settings.sse)
    stream_store.init(redis.client)
    pubsub = RedisPubSub(settings.sse)
    pubsub.init(redis.client)

    # Auth service — built after Mongo connects; reads from app.state on dispatch.
    from trendstorm.infrastructure.auth.jwt import IdPConfig, JWTValidator
    from trendstorm.infrastructure.mongo.repositories.api_key_repository import (
        MongoApiKeyRepository,
    )
    from trendstorm.infrastructure.mongo.repositories.tenant_repository import MongoTenantRepository
    from trendstorm.services.auth.service import AuthService

    jwt_validator = None
    if settings.auth.jwt_issuer_url and settings.auth.jwt_audience:
        jwt_validator = JWTValidator(
            [IdPConfig(settings.auth.jwt_issuer_url, settings.auth.jwt_audience)]
        )

    auth_service = AuthService(
        api_key_repo=MongoApiKeyRepository(mongo),
        tenant_repo=MongoTenantRepository(mongo),
        jwt_validator=jwt_validator,
        settings=settings.auth,
    )

    # Attach to app.state for DI
    app.state.mongo = mongo
    app.state.redis = redis
    app.state.kafka_producer = kafka_producer
    app.state.blob = blob
    app.state.vector_store = vector_store
    app.state.embedding_provider = embedding_provider
    app.state.stream_store = stream_store
    app.state.pubsub = pubsub
    app.state.auth_service = auth_service

    logger.info("app_started")

    try:
        yield  # ---- Serving phase: requests are handled here ---------
    finally:
        # ---- Shutdown ---------------------------------------------------
        logger.info("app_stopping")
        await _safe_shutdown(mongo, redis, kafka_producer, blob, vector_store)
        shutdown_tracing()
        logger.info("app_stopped")


async def _safe_shutdown(
    mongo: MongoClient,
    redis: RedisClient,
    kafka_producer: KafkaProducerClient,
    blob: MinioClient,
    vector_store: ChromaVectorStore,
) -> None:
    """Stop all clients, swallowing exceptions to ensure full cleanup."""
    results = await asyncio.gather(
        kafka_producer.stop(),
        redis.close(),
        mongo.close(),
        blob.close(),
        vector_store.close(),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, Exception):
            logger.warning("shutdown_step_failed", error=str(result))


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory.

    Args:
        settings: Override the default Settings. Tests pass a custom Settings;
            production calls with no args (uses cached Settings from env).

    Returns:
        A configured FastAPI instance ready to serve.

    """
    settings = settings or get_settings()

    # Logging FIRST — startup logs need to be structured too.
    configure_logging()
    configure_tracing()

    app = FastAPI(
        title="TrendStorm AI",
        version=__version__,
        description="Autonomous multi-agent trend intelligence and market research.",
        lifespan=lifespan,
        # We provide our own validation error handler; disable FastAPI's default
        # to keep response shape consistent.
        # (FastAPI uses RequestValidationError; we install our own handler below.)
    )
    # Stash settings on app.state so lifespan can read them without re-resolving.
    app.state.settings = settings

    # ---- Middleware (registered in REVERSE order of execution) -----------
    # Inner (closest to handler) -> Outer (closest to network)
    # Adding A then B -> B wraps A -> request flow: B -> A -> handler -> A -> B
    #
    # Execution order: CorrelationId → Auth → RateLimit → RequestLogging → Tenant → CORS
    # Registration order (last-added runs first):
    #   CORS, Tenant, RequestLogging, RateLimit, Auth, CorrelationId
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-correlation-id"],
    )
    app.add_middleware(TenantMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(
        RateLimitMiddleware,
        settings=settings.auth,
    )
    app.add_middleware(
        AuthMiddleware,
        settings=settings.auth,
        app_env=settings.app.env,
    )
    app.add_middleware(CorrelationIdMiddleware)

    # ---- Routers ----------------------------------------------------------
    app.include_router(health_router.router)
    app.include_router(metrics_router.router)
    app.include_router(auth_router.router)
    app.include_router(users_router.router)
    app.include_router(organizations_router.router)
    app.include_router(memberships_router.router)
    app.include_router(invites_router.router)
    app.include_router(categories_router.router)
    app.include_router(sources_router.router)
    app.include_router(jobs_router.router)
    app.include_router(api_keys_router.router)
    app.include_router(quota_router.router)
    app.include_router(reviews_router.router)
    app.include_router(memories_router.router)
    app.include_router(audit_router.router)

    # ---- Exception handlers ----------------------------------------------
    install_exception_handlers(app)

    # ---- OTel auto-instrumentation for the FastAPI app -------------------
    instrument_fastapi(app)

    return app


# Module-level ASGI app for `uvicorn trendstorm.api.main:app`.
# In production we'd often pass `create_app` as a factory:
#   uvicorn --factory trendstorm.api.main:create_app
# but for simplicity (and to match most tooling) we expose `app` directly.
app = create_app()
