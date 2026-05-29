"""LLM cost attribution helpers.

record_llm_cost() is the ONLY place that increments token counters.
Call it after every LLM API call (chat completion or embedding batch)
regardless of success/failure — partial calls still consume tokens.

Prometheus counters (Grafana) are always updated. When `job_id` and
`ledger` are supplied, a `CostLedgerEntry` is also persisted to Mongo as
a fire-and-forget async task (billing reconciliation). The Mongo write
failure never crashes the business logic — cost recording is best-effort.

Why separate from the metrics registry?
    Callers import this module and call record_llm_cost(); they never
    construct label dicts manually. The function owns the label mapping
    logic so call sites stay clean.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from trendstorm.shared.logging import get_logger
from trendstorm.shared.metrics.registry import METRICS

if TYPE_CHECKING:
    from trendstorm.domain.billing.repository import CostLedgerRepository

logger = get_logger(__name__)


def record_llm_cost(
    *,
    tenant_id: str,
    provider: str,
    model_id: str,
    operation: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    call_duration_seconds: float | None = None,
    success: bool = True,
    # Billing ledger (optional) — supply job_id + ledger to persist to Mongo.
    job_id: str | None = None,
    ledger: CostLedgerRepository | None = None,
) -> None:
    """Increment LLM token counters and (optionally) persist a cost ledger entry.

    Args:
        tenant_id:             Tenant this call is billed to.
        provider:              One of: anthropic, openai, ollama, gemini, cohere.
        model_id:              Model string as returned by the API.
        operation:             Purpose: "analyst_chat", "validator_chat",
                               "query_expansion", "embed_document", "embed_query",
                               "rerank". Maps to LedgerStage when persisting.
        input_tokens:          Prompt tokens consumed.
        output_tokens:         Completion tokens generated.
        cached_tokens:         Anthropic prompt-cache read tokens (subset of input).
        call_duration_seconds: Elapsed seconds for the API call; passed to histogram.
        success:               False records "permanent_error" on the call counter.
        job_id:                When provided (with `ledger`), persists a CostLedgerEntry.
        ledger:                CostLedgerRepository to write billing entries into.

    """
    status = "success" if success else "permanent_error"
    lkw = {
        "tenant_id": tenant_id,
        "provider": provider,
        "model_id": model_id,
        "operation": operation,
    }
    try:
        METRICS.llm_calls.labels(**lkw, status=status).inc()
        METRICS.llm_input_tokens.labels(**lkw).inc(input_tokens)
        METRICS.llm_output_tokens.labels(**lkw).inc(output_tokens)
        if cached_tokens:
            METRICS.llm_cached_tokens.labels(**lkw).inc(cached_tokens)
        if call_duration_seconds is not None:
            METRICS.llm_call_duration.labels(**lkw).observe(call_duration_seconds)
    except Exception as exc:
        # Never crash business logic due to metric recording failure.
        logger.warning("llm_cost_record_failed", error=str(exc))

    # Persist to cost ledger if both job_id and ledger are supplied.
    if job_id is not None and ledger is not None:
        _schedule_ledger_write(
            tenant_id=tenant_id,
            job_id=job_id,
            provider=provider,
            model_id=model_id,
            operation=operation,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            ledger=ledger,
        )


def _schedule_ledger_write(
    *,
    tenant_id: str,
    job_id: str,
    provider: str,
    model_id: str,
    operation: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    ledger: CostLedgerRepository,
) -> None:
    """Fire-and-forget async ledger write. Only valid inside an async context."""
    from trendstorm.domain.billing.models import CostLedgerEntry
    from trendstorm.services.billing.prices import compute_cost_usd_micro

    # Map operation string → LedgerStage (best-effort; unknown ops map to a safe default)
    _OP_TO_STAGE = {  # noqa: N806  # module-level constant defined inside function
        "analyst_chat": "analysis_analyst",
        "validator_chat": "analysis_validator",
        "query_expansion": "query_expansion",
        "rerank": "rerank",
        "embed_document": "embedding",
        "embed_query": "embedding",
    }
    stage = _OP_TO_STAGE.get(operation, "analysis_analyst")

    cost = compute_cost_usd_micro(
        provider=provider,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
    )
    entry = CostLedgerEntry(
        tenant_id=tenant_id,
        job_id=job_id,
        stage=stage,
        provider=provider,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        cost_usd_micro=cost,
    )

    async def _write() -> None:
        try:
            await ledger.insert(entry)
        except Exception as exc:
            logger.warning("cost_ledger_write_failed", error=str(exc))

    try:
        loop = asyncio.get_running_loop()
        _task = loop.create_task(_write())  # noqa: RUF006  # fire-and-forget; task reference kept to prevent GC
    except RuntimeError:
        # No running event loop (e.g. sync test context) — skip silently.
        pass
