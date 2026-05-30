"""Account purge sweeper — GDPR hard-delete for tombstoned user accounts.

The sweeper runs as a standalone polling loop (no Kafka input topic). Every
`poll_interval_seconds` (default: 3600 = 1 hour) it finds users whose
`purge_at <= now()` and whose `deleted_at IS NOT NULL`, then calls
AccountDeletionService.execute_purge() for each.

Scaling:
    Always deploy exactly 1 replica (`strategy: Recreate`). Two replicas do not
    corrupt data — execute_purge is idempotent (hard_delete is a no-op if already
    gone) — but they would double-log and double-call the IdP delete. Keep it
    single-replica.

Prometheus alert:
    No dedicated alert. If the sweeper fails, the next poll handles backlog.
    A PagerDuty alert on Python exceptions (via log-based alerting) covers
    persistent failures.
"""

from __future__ import annotations

import asyncio
import signal

from trendstorm.infrastructure.auth.auth0_provider import Auth0Provider
from trendstorm.infrastructure.email.dev_provider import DevEmailProvider
from trendstorm.infrastructure.email.email_provider import EmailProvider
from trendstorm.infrastructure.email.postmark_provider import PostmarkProvider
from trendstorm.infrastructure.metrics.prometheus_server import MetricsServer
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories.membership_repository import (
    MongoMembershipRepository,
)
from trendstorm.infrastructure.mongo.repositories.organization_repository import (
    MongoOrganizationRepository,
)
from trendstorm.infrastructure.mongo.repositories.session_repository import (
    MongoRefreshSessionRepository,
)
from trendstorm.infrastructure.mongo.repositories.user_repository import MongoUserRepository
from trendstorm.infrastructure.redis.client import RedisClient
from trendstorm.services.auth.account_deletion_service import AccountDeletionService
from trendstorm.services.auth.session_service import SessionService
from trendstorm.shared.config import get_settings
from trendstorm.shared.logging import configure_logging, get_logger
from trendstorm.shared.tracing import configure_tracing, shutdown_tracing

logger = get_logger(__name__)

_DEFAULT_POLL_INTERVAL = 3600  # 1 hour
_DEFAULT_BATCH_SIZE = 50


class AccountPurgeSweeper:
    """Polling loop that hard-deletes tombstoned accounts past their purge_at."""

    def __init__(
        self,
        *,
        deletion_svc: AccountDeletionService,
        user_repo: MongoUserRepository,
        poll_interval_seconds: int = _DEFAULT_POLL_INTERVAL,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> None:
        self._deletion_svc = deletion_svc
        self._users = user_repo
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size

    async def sweep_loop(self, *, stop_event: asyncio.Event) -> None:
        logger.info("account_purge_sweeper.started", interval_s=self._poll_interval)
        while not stop_event.is_set():
            try:
                await self._sweep_once()
            except Exception:
                logger.exception("account_purge_sweeper.error")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval)
            except TimeoutError:
                pass

        logger.info("account_purge_sweeper.stopped")

    async def _sweep_once(self) -> None:
        users = await self._users.list_due_for_purge(limit=self._batch_size)
        if not users:
            return
        logger.info("account_purge_sweeper.sweep", count=len(users))
        for user in users:
            try:
                await self._deletion_svc.execute_purge(user)
                logger.info("account_purge_sweeper.purged", user_id=user.id)
            except Exception:
                logger.exception("account_purge_sweeper.purge_failed", user_id=user.id)


async def run_worker() -> None:
    configure_logging()
    configure_tracing()
    settings = get_settings()

    mongo = MongoClient(settings.mongo)
    redis = RedisClient(settings.redis)
    await asyncio.gather(mongo.connect(), redis.connect())

    user_repo = MongoUserRepository(mongo)
    membership_repo = MongoMembershipRepository(mongo)
    org_repo = MongoOrganizationRepository(mongo)
    session_repo = MongoRefreshSessionRepository(mongo)
    session_svc = SessionService(
        session_repo=session_repo,
        user_repo=user_repo,
        membership_repo=membership_repo,
        redis=redis,
        jwt_settings=settings.jwt,
    )
    email_provider: EmailProvider
    if settings.email.provider == "postmark":
        email_provider = PostmarkProvider(settings.email)
    else:
        email_provider = DevEmailProvider()

    deletion_svc = AccountDeletionService(
        user_repo=user_repo,
        membership_repo=membership_repo,
        org_repo=org_repo,
        session_service=session_svc,
        identity_provider=Auth0Provider(settings.auth0),
        email_provider=email_provider,
        signup_settings=settings.signup,
        email_settings=settings.email,
    )

    sweeper = AccountPurgeSweeper(
        deletion_svc=deletion_svc,
        user_repo=user_repo,
    )

    metrics_server = MetricsServer(port=settings.kafka.metrics_port)
    await metrics_server.start()

    stop_event = asyncio.Event()

    def _handle_signal(sig: int, _frame: object) -> None:
        logger.info("account_purge_worker.signal_received", signal=sig)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        await sweeper.sweep_loop(stop_event=stop_event)
    finally:
        await metrics_server.stop()
        await asyncio.gather(redis.close(), mongo.close(), return_exceptions=True)
        shutdown_tracing()
        logger.info("account_purge_worker.stopped")


if __name__ == "__main__":
    asyncio.run(run_worker())
