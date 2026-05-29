"""Health check endpoints.

Liveness vs Readiness — the distinction matters in Kubernetes:

    /health/live   "Am I alive?"        Fast, no I/O.
                                        Failure -> K8s kills the pod.

    /health/ready  "Should I serve?"    Checks all dependencies.
                                        Failure -> K8s removes from LB
                                        (doesn't kill).

Collapsing them into one endpoint is a classic mistake:
    - Mongo blip -> /health fails -> all pods restart -> cascading outage.
    - Separate probes: liveness still OK, readiness flaps off, traffic
      shifts to healthy pods, no restarts.

A third endpoint, /health/startup (not implemented here), is useful for slow-starting apps:
    - K8s won't probe liveness until startup probe passes.
    - Prevents kill-during-init for apps that take >30s to be ready.
    - Our app starts in <5s, so we skip this.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Awaitable

from fastapi import APIRouter, Response
from pydantic import BaseModel

from trendstorm.api.deps import (
    BlobDep,
    KafkaDep,
    MongoDep,
    RedisDep,
    SettingsDep,
    VectorStoreDep,
)

router = APIRouter(prefix="/health", tags=["health"])


# --- Response models ---------------------------------------------------------


class LiveResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ComponentStatus(BaseModel):
    name: str
    healthy: bool
    detail: str | None = None


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    components: list[ComponentStatus]


# --- Endpoints ---------------------------------------------------------------


@router.get("/live", response_model=LiveResponse)
async def liveness() -> LiveResponse:
    """Liveness probe: is the process alive.

    Intentionally trivial. If this endpoint can't return, the event loop is
    blocked, the process is wedged, or something equally bad happened — and
    K8s should restart us.
    """
    return LiveResponse()


@router.get("/ready", response_model=ReadyResponse)
async def readiness(
    response: Response,
    settings: SettingsDep,
    mongo: MongoDep,
    redis: RedisDep,
    kafka: KafkaDep,
    blob: BlobDep,
    vector_store: VectorStoreDep,
) -> ReadyResponse:
    """Readiness probe: are all dependencies reachable.

    Runs health checks in parallel for fast probes. Failure returns 503 so
    K8s removes us from the service endpoints; the pod stays alive.
    """
    mongo_ok, redis_ok, kafka_ok, blob_ok, vector_ok = await asyncio.gather(
        _bounded_health(mongo.health_check(), deadline=2.0),
        _bounded_health(redis.health_check(), deadline=2.0),
        _bounded_health(kafka.health_check(), deadline=2.0),
        _bounded_health(blob.health_check(), deadline=2.0),
        _bounded_health(vector_store.health_check(), deadline=2.0),
        return_exceptions=False,
    )

    components = [
        ComponentStatus(name="mongo", healthy=mongo_ok),
        ComponentStatus(name="redis", healthy=redis_ok),
        ComponentStatus(name="kafka", healthy=kafka_ok),
        ComponentStatus(name="blob", healthy=blob_ok),
        ComponentStatus(name="vector_store", healthy=vector_ok),
    ]
    all_ok = all(c.healthy for c in components)

    if not all_ok:
        response.status_code = 503
        return ReadyResponse(status="not_ready", components=components)

    return ReadyResponse(status="ready", components=components)


# --- Helpers ----------------------------------------------------------------


async def _bounded_health(check_coro: Awaitable[bool], deadline: float) -> bool:
    """Run a health check with a hard timeout; never raises."""
    try:
        return await asyncio.wait_for(check_coro, timeout=deadline)
    except (TimeoutError, Exception):
        return False
