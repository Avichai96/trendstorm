"""Kafka async producer wrapper.

The API service only produces events (it publishes `jobs.requested` when a
user creates a job). Consumer infrastructure lives with the workers
(orchestration/) and ships in Phase 4+.

Why a wrapper?
    - Lifecycle: start/stop tied to FastAPI lifespan.
    - Configuration: centralized producer config (acks, compression, idempotence).
    - Observability: we can wrap send() with metrics later without changing
      callers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiokafka import AIOKafkaProducer
from aiokafka.errors import KafkaError

from trendstorm.shared.config import KafkaSettings
from trendstorm.shared.errors import BrokerError
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


class KafkaProducerClient:
    """Async Kafka producer lifecycle manager."""

    def __init__(self, settings: KafkaSettings) -> None:
        self._settings = settings
        self._producer: AIOKafkaProducer | None = None

    async def start(self) -> None:
        """Initialize and start the producer. Idempotent."""
        if self._producer is not None:
            return

        logger.info("kafka_producer_starting", bootstrap=self._settings.bootstrap_servers)

        # Producer config — these settings encode the durability triangle:
        #   acks="all" + enable_idempotence=True = exactly-once-per-producer-session
        # In production, replication.factor must be >=3 and min.insync.replicas>=2
        # for these guarantees to hold; in dev with RF=1 we still get the API
        # behavior but the durability promise is weaker (single broker = SPOF).
        producer_kwargs: dict[str, object] = {
            "bootstrap_servers": self._settings.bootstrap_servers,
            "client_id": f"{self._settings.client_id}-producer",
            "acks": "all",
            "enable_idempotence": True,
            "compression_type": "lz4",  # matches kafka-init topic config
            # 5 in-flight requests is the max for idempotent producer
            "max_batch_size": 64 * 1024,
            "linger_ms": 20,  # small wait to allow batching
            "request_timeout_ms": 30000,
        }

        if self._settings.is_secure:
            producer_kwargs["security_protocol"] = self._settings.security_protocol
            if self._settings.sasl_mechanism:
                producer_kwargs["sasl_mechanism"] = self._settings.sasl_mechanism
            if self._settings.sasl_username and self._settings.sasl_password:
                producer_kwargs["sasl_plain_username"] = self._settings.sasl_username
                producer_kwargs["sasl_plain_password"] = (
                    self._settings.sasl_password.get_secret_value()
                )

        self._producer = AIOKafkaProducer(**producer_kwargs)
        try:
            await self._producer.start()
        except KafkaError as e:
            self._producer = None
            raise BrokerError(
                "Kafka producer failed to start",
                context={"error": str(e), "error_type": type(e).__name__},
            ) from e

        logger.info("kafka_producer_started")

    async def stop(self) -> None:
        """Stop the producer, flushing pending messages. Idempotent."""
        if self._producer is None:
            return
        logger.info("kafka_producer_stopping")
        try:
            await self._producer.stop()
        except KafkaError as e:
            logger.warning("kafka_producer_stop_error", error=str(e))
        finally:
            self._producer = None

    @property
    def producer(self) -> AIOKafkaProducer:
        """The underlying producer. Raises if not started."""
        if self._producer is None:
            raise BrokerError("Kafka producer not started; call start() first")
        return self._producer

    async def health_check(self) -> bool:
        """Return True if the producer is still attached to the broker.

        aiokafka doesn't expose a clean health primitive; we check the
        producer state. A more thorough check would query cluster metadata,
        but that adds latency to readiness probes.
        """
        if self._producer is None:
            return False
        # `_closed` is a private flag but the public API doesn't expose state.
        # If aiokafka changes this, we'll fall back to a metadata query.
        return not getattr(self._producer, "_closed", True)
