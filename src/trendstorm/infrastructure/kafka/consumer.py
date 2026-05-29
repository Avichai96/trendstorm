"""Base Kafka consumer for TrendStorm workers.

Encapsulates patterns that every worker needs:
    - Manual offset management (commit AFTER successful processing).
    - Idempotency check before invoking handler.
    - Graceful shutdown on SIGTERM.
    - Error handling with retry-topic routing.
    - OTel span per message.
    - Correlation ID propagation from the event envelope.

The handler subclass overrides one method: `handle(event)`.

Why manual offset commits?
    auto-commit can ack a message BEFORE the handler finishes. If the worker
    crashes mid-handler, the message is silently lost. Manual commit happens
    only after `handle()` returns successfully, ensuring at-least-once
    semantics (which our idempotency layer turns into effectively exactly-once).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from abc import abstractmethod
from typing import TYPE_CHECKING

from aiokafka import AIOKafkaConsumer
from aiokafka.errors import KafkaError
from opentelemetry import trace
from opentelemetry.propagate import extract
from pydantic import TypeAdapter, ValidationError

from trendstorm.infrastructure.kafka.producer import KafkaProducerClient
from trendstorm.infrastructure.metrics.prometheus_server import MetricsServer
from trendstorm.infrastructure.mongo.repositories import (
    IdempotencyRepository,
    IdempotencyResult,
)
from trendstorm.orchestration.events import AnyEvent, EventEnvelope
from trendstorm.orchestration.topics import Topic
from trendstorm.shared.config import KafkaSettings
from trendstorm.shared.errors import TrendStormError
from trendstorm.shared.logging import bind_context, get_logger

if TYPE_CHECKING:
    from aiokafka.structs import ConsumerRecord


logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


_event_adapter: TypeAdapter[AnyEvent] = TypeAdapter(AnyEvent)


class ConsumerStopped(Exception):  # noqa: N818  # signals stop, not an error
    """Raised internally to break the consume loop on shutdown."""


class BaseConsumer:
    """Base Kafka consumer with offset/idempotency/error machinery built in."""

    def __init__(
        self,
        *,
        topics: list[Topic],
        group_id: str,
        settings: KafkaSettings,
        idempotency: IdempotencyRepository,
        producer: KafkaProducerClient,
        worker_name: str,
    ) -> None:
        self._topics = topics
        self._group_id = group_id
        self._settings = settings
        self._idempotency = idempotency
        self._producer = producer
        self._worker_name = worker_name

        self._consumer: AIOKafkaConsumer | None = None
        self._stop_event = asyncio.Event()
        self._installed_signals = False
        self._metrics_server = MetricsServer(port=settings.metrics_port)

    # ----------------------------------------------------------------- #
    # Lifecycle                                                          #
    # ----------------------------------------------------------------- #

    async def start(self) -> None:
        """Connect to Kafka, join the consumer group, and start the metrics server."""
        if self._consumer is not None:
            return
        await self._metrics_server.start()
        kwargs: dict[str, object] = {
            "bootstrap_servers": self._settings.bootstrap_servers,
            "group_id": self._group_id,
            "client_id": f"{self._settings.client_id}-{self._worker_name}",
            "enable_auto_commit": False,  # we commit manually
            "auto_offset_reset": "earliest",
            # Limit in-flight: 1 message per partition at a time, processed
            # sequentially. Within a partition, ordering matters (per-job
            # events must be processed in order).
            "max_poll_records": 50,
            # If a consumer doesn't poll within this window, the group
            # rebalances. We set it long enough to cover slow LLM calls.
            "session_timeout_ms": 60_000,
            "heartbeat_interval_ms": 15_000,
        }
        if self._settings.is_secure:
            kwargs["security_protocol"] = self._settings.security_protocol
            if self._settings.sasl_mechanism:
                kwargs["sasl_mechanism"] = self._settings.sasl_mechanism
            if self._settings.sasl_username and self._settings.sasl_password:
                kwargs["sasl_plain_username"] = self._settings.sasl_username
                kwargs["sasl_plain_password"] = self._settings.sasl_password.get_secret_value()

        self._consumer = AIOKafkaConsumer(*[t.value for t in self._topics], **kwargs)
        await self._consumer.start()
        logger.info(
            "consumer_started",
            worker=self._worker_name,
            group_id=self._group_id,
            topics=[t.value for t in self._topics],
        )

    async def stop(self) -> None:
        """Stop the consumer and metrics server."""
        if self._consumer is None:
            return
        logger.info("consumer_stopping", worker=self._worker_name)
        self._stop_event.set()
        try:
            await self._consumer.stop()
        except KafkaError as e:
            logger.warning("consumer_stop_error", error=str(e))
        finally:
            self._consumer = None
        await self._metrics_server.stop()

    def install_signal_handlers(self) -> None:
        """Wire SIGTERM/SIGINT to trigger graceful shutdown.

        Called once at process start. Sets `_stop_event` so the consume loop
        exits cleanly after the current message finishes.
        """
        if self._installed_signals:
            return
        loop = asyncio.get_running_loop()

        def _handle_signal(sig: int) -> None:
            logger.info("signal_received", signal=sig)
            self._stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _handle_signal, sig)
            except NotImplementedError:
                # Windows: signal.signal fallback
                signal.signal(sig, lambda s, f: _handle_signal(s))
        self._installed_signals = True

    # ----------------------------------------------------------------- #
    # Main loop                                                          #
    # ----------------------------------------------------------------- #

    async def run(self) -> None:
        """Consume messages until stopped.

        Loop body:
            1. Receive a batch.
            2. For each message: parse, idempotency-check, handle, commit.
            3. On any error: log, route to retry topic or DLQ.
        """
        if self._consumer is None:
            raise RuntimeError("Consumer not started; call start() first")

        try:
            while not self._stop_event.is_set():
                # `getmany` returns a dict {TopicPartition: [records]}.
                # We use a short timeout so we wake up to check _stop_event.
                batch = await self._consumer.getmany(timeout_ms=1000, max_records=50)
                if self._stop_event.is_set():
                    break
                for tp, records in batch.items():
                    for record in records:
                        if self._stop_event.is_set():
                            break
                        await self._process_record(record)
                        # Commit AFTER each record so a crash doesn't reprocess
                        # already-finished work (idempotency handles the rest).
                        await self._consumer.commit({tp: record.offset + 1})
        except ConsumerStopped:
            pass
        finally:
            logger.info("consumer_loop_exited", worker=self._worker_name)

    # ----------------------------------------------------------------- #
    # Per-record processing                                              #
    # ----------------------------------------------------------------- #

    async def _process_record(self, record: ConsumerRecord) -> None:
        """Parse, idempotency-check, and dispatch a single record.

        Errors here are NEVER re-raised — they're routed to a retry topic
        or DLQ. Re-raising would block the partition forever (poison message).
        """
        # 1. Parse — bad JSON = poison pill, send to DLQ.
        try:
            event = _event_adapter.validate_json(record.value)
        except (ValidationError, ValueError) as e:
            logger.error(
                "parse_failed_to_dlq",
                topic=record.topic,
                partition=record.partition,
                offset=record.offset,
                error=str(e),
            )
            await self._send_to_dlq(record.value, reason="parse_error", detail=str(e))
            return

        # 2. Bind context for all downstream logs.
        bind_context(
            correlation_id=event.correlation_id,
            tenant_id=event.tenant_id,
        )

        # 3. Start a span. Extract trace context from the event envelope
        # so this span continues the trace started by the producer.
        carrier = {"traceparent": event.traceparent} if event.traceparent else {}
        ctx = extract(carrier)

        with tracer.start_as_current_span(
            f"consume {record.topic}",
            context=ctx,
            attributes={
                "messaging.system": "kafka",
                "messaging.destination.name": record.topic,
                "messaging.kafka.consumer.group": self._group_id,
                "messaging.kafka.partition": record.partition,
                "messaging.kafka.offset": record.offset,
                "trendstorm.event_type": event.event_type,
                "trendstorm.tenant_id": event.tenant_id,
            },
        ):
            try:
                await self._dispatch_with_idempotency(event)
            except TrendStormError as e:
                # Domain error — known shape. Log + DLQ (or retry, depending
                # on subclass routing logic).
                logger.warning(
                    "handler_domain_error",
                    error_code=e.code,
                    error_message=e.message,
                    event_type=event.event_type,
                )
                await self._handle_failure(event, e)
            except Exception as e:
                # Unknown failure — log with traceback and DLQ.
                logger.exception("handler_unexpected_error", event_type=event.event_type)
                await self._send_to_dlq(record.value, reason="handler_exception", detail=str(e))

    async def _dispatch_with_idempotency(self, event: EventEnvelope) -> None:
        """Acquire idempotency, call handler, mark complete."""
        key = self._idempotency_key(event)
        if key is None:
            # Some event types are intentionally non-idempotent (e.g. stream
            # partials). Subclass returns None to skip the check.
            await self._timed_handle(event)
            return

        ack: IdempotencyResult = await self._idempotency.acquire(key)
        if not ack.acquired:
            logger.info("idempotency_skip", key=key)
            return

        try:
            await self._timed_handle(event)
        except Exception:
            # Release the key so a retry can re-acquire.
            await self._idempotency.release(key)
            raise

        await self._idempotency.complete(key)

    async def _timed_handle(self, event: EventEnvelope) -> None:
        """Wrap handle() with elapsed timing and call _record_handle_metrics."""
        start = time.perf_counter()
        status = "success"
        try:
            await self.handle(event)
        except Exception:
            status = "error"
            raise
        finally:
            elapsed = time.perf_counter() - start
            with contextlib.suppress(Exception):
                self._record_handle_metrics(event, status, elapsed)

    def _record_handle_metrics(self, event: EventEnvelope, status: str, elapsed: float) -> None:
        """Override to record per-event worker metrics. Default: no-op.

        Called after every handle() invocation with the outcome status
        ("success" or "error") and elapsed seconds. Subclasses record to their
        specific METRICS counters/histograms here rather than duplicating timing
        logic in handle().
        """

    async def _send_to_dlq(self, payload: bytes, *, reason: str, detail: str) -> None:
        """Send a poisoned message to the DLQ with diagnostic headers."""
        try:
            await self._producer.producer.send_and_wait(
                Topic.DLQ.value,
                value=payload,
                headers=[
                    ("x-dlq-reason", reason.encode()),
                    ("x-dlq-detail", detail[:500].encode()),
                    ("x-worker-name", self._worker_name.encode()),
                ],
            )
        except KafkaError as e:
            # If we can't even DLQ, we're in a bad state. Log loudly.
            logger.critical("dlq_send_failed", error=str(e), reason=reason)

    # ----------------------------------------------------------------- #
    # Subclass hooks                                                     #
    # ----------------------------------------------------------------- #

    @abstractmethod
    async def handle(self, event: EventEnvelope) -> None:
        """Process a single parsed event. Subclasses implement this."""

    def _idempotency_key(self, event: EventEnvelope) -> str | None:
        """Compute an idempotency key for this event. Return None to opt out.

        Default: derive from event_id (every produce = unique key). Subclasses
        often override to use {job_id, stage, attempt} for true exactly-once.
        """
        return f"{self._worker_name}:{event.event_id}"

    async def _handle_failure(self, event: EventEnvelope, error: TrendStormError) -> None:
        """Route domain errors to retry topic or DLQ.

        Default behavior: send to DLQ. Subclasses override to route
        retryable errors to retry topics with backoff.
        """
        await self._send_to_dlq(
            event.model_dump_json().encode(),
            reason=error.code,
            detail=error.message,
        )
