"""FastAPI dependency injection providers.

The DI surface for the entire API. Every router declares what it needs:

    @router.post("/jobs")
    async def create_job(
        body: CreateJobRequest,
        mongo: Annotated[MongoClient, Depends(get_mongo)],
        kafka: Annotated[KafkaProducerClient, Depends(get_kafka_producer)],
    ) -> JobResponse:
        ...

Why this pattern?
    - Testability: tests override `get_mongo` with a mock via
      `app.dependency_overrides[get_mongo] = lambda: fake_mongo`.
    - Explicitness: function signature documents dependencies.
    - Lazy: dependencies created when needed, not at import time.
    - Singleton control: lifespan owns the actual instances; providers just
      return them.

State storage:
    Singletons (Mongo, Kafka, Redis clients) are stored on `app.state` during
    lifespan startup. Providers read from `request.app.state`. This avoids
    module-level mutable state, which is hostile to testing and confusing
    in worker processes that don't have a FastAPI app.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from trendstorm.infrastructure.blob.minio_client import MinioClient
from trendstorm.infrastructure.kafka.producer import KafkaProducerClient
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.redis.client import RedisClient
from trendstorm.infrastructure.redis.pubsub import RedisPubSub
from trendstorm.infrastructure.redis.streams import RedisStreamStore
from trendstorm.infrastructure.vectors.chroma_store import ChromaVectorStore
from trendstorm.services.auth.service import AuthService
from trendstorm.shared.config import Settings, get_settings

# ---------------------------------------------------------------------------
# Configuration provider
# ---------------------------------------------------------------------------

def get_app_settings() -> Settings:
    """Provide the cached app Settings."""
    return get_settings()


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


# ---------------------------------------------------------------------------
# Infrastructure client providers
# ---------------------------------------------------------------------------

def get_mongo(request: Request) -> MongoClient:
    """Provide the Mongo client from app.state.

    Raises 500 if the client isn't initialized — which means lifespan startup
    failed but the app is somehow serving requests. Should never happen in
    practice; if it does, that's a bug in startup ordering.
    """
    client: MongoClient | None = getattr(request.app.state, "mongo", None)
    if client is None:
        raise RuntimeError("Mongo client missing from app.state — startup misconfigured")
    return client


def get_redis(request: Request) -> RedisClient:
    """Provide the Redis client from app.state."""
    client: RedisClient | None = getattr(request.app.state, "redis", None)
    if client is None:
        raise RuntimeError("Redis client missing from app.state — startup misconfigured")
    return client


def get_kafka_producer(request: Request) -> KafkaProducerClient:
    """Provide the Kafka producer from app.state."""
    client: KafkaProducerClient | None = getattr(request.app.state, "kafka_producer", None)
    if client is None:
        raise RuntimeError("Kafka producer missing from app.state — startup misconfigured")
    return client


# ---------------------------------------------------------------------------
# Convenient type aliases for router signatures
# ---------------------------------------------------------------------------

def get_blob(request: Request) -> MinioClient:
    """Provide the MinIO/blob client from app.state."""
    client: MinioClient | None = getattr(request.app.state, "blob", None)
    if client is None:
        raise RuntimeError("Blob client missing from app.state — startup misconfigured")
    return client


def get_vector_store(request: Request) -> ChromaVectorStore:
    """Provide the ChromaDB vector store from app.state."""
    client: ChromaVectorStore | None = getattr(request.app.state, "vector_store", None)
    if client is None:
        raise RuntimeError("Vector store missing from app.state — startup misconfigured")
    return client


def get_stream_store(request: Request) -> RedisStreamStore:
    """Provide the RedisStreamStore from app.state."""
    store: RedisStreamStore | None = getattr(request.app.state, "stream_store", None)
    if store is None:
        raise RuntimeError("RedisStreamStore missing from app.state — startup misconfigured")
    return store


def get_pubsub(request: Request) -> RedisPubSub:
    """Provide the RedisPubSub from app.state."""
    ps: RedisPubSub | None = getattr(request.app.state, "pubsub", None)
    if ps is None:
        raise RuntimeError("RedisPubSub missing from app.state — startup misconfigured")
    return ps


def get_auth_service(request: Request) -> AuthService:
    """Provide the AuthService from app.state."""
    svc: AuthService | None = getattr(request.app.state, "auth_service", None)
    if svc is None:
        raise RuntimeError("AuthService missing from app.state — startup misconfigured")
    return svc


MongoDep = Annotated[MongoClient, Depends(get_mongo)]
RedisDep = Annotated[RedisClient, Depends(get_redis)]
KafkaDep = Annotated[KafkaProducerClient, Depends(get_kafka_producer)]
BlobDep = Annotated[MinioClient, Depends(get_blob)]
VectorStoreDep = Annotated[ChromaVectorStore, Depends(get_vector_store)]
StreamStoreDep = Annotated[RedisStreamStore, Depends(get_stream_store)]
PubSubDep = Annotated[RedisPubSub, Depends(get_pubsub)]
AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
