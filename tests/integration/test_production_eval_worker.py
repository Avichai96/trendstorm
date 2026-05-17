"""Integration tests for ProductionEvalWorker dispatch logic.

These tests verify:
  - idempotency key format
  - event routing (EvalSampleEvent dispatched to pipeline)
  - unexpected event types are logged and discarded
  - skipped analyses do not raise

No real Kafka, Mongo, or Docker required — all dependencies mocked.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from trendstorm.agents.production_eval.pipeline import ProductionEvalPipeline, ProductionEvalResult
from trendstorm.domain.evaluation.models import EvalDimension, EvaluationResult
from trendstorm.orchestration.events import EvalSampleEvent
from trendstorm.orchestration.workers.production_eval_worker import ProductionEvalWorker
from trendstorm.shared.ids import new_id

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(
    job_id: str = "j1",
    analysis_id: str = "a1",
    tenant_id: str = "t1",
) -> EvalSampleEvent:
    return EvalSampleEvent(
        event_id=new_id(),
        correlation_id=new_id(),
        tenant_id=tenant_id,
        job_id=job_id,
        analysis_id=analysis_id,
    )


def _make_eval_result(tenant_id: str = "t1") -> EvaluationResult:
    from trendstorm.domain.evaluation.models import DimensionScore
    ds = DimensionScore(
        dimension=EvalDimension.FAITHFULNESS,
        score=0.9,
        passed=True,
    )
    return EvaluationResult(
        tenant_id=tenant_id,
        analysis_id="a1",
        job_id="j1",
        dimension_scores=[ds],
        aggregate_score=0.9,
    )


def _make_worker(pipeline: ProductionEvalPipeline) -> ProductionEvalWorker:
    kafka_settings = MagicMock()
    kafka_settings.bootstrap_servers = "kafka:9092"
    kafka_settings.consumer_group = "test-eval"
    kafka_settings.metrics_port = 9090

    idem = MagicMock()
    producer = MagicMock()

    worker = ProductionEvalWorker.__new__(ProductionEvalWorker)
    worker._pipeline = pipeline
    worker._producer = producer
    worker._idempotency = idem
    return worker


# ---------------------------------------------------------------------------
# Idempotency key tests
# ---------------------------------------------------------------------------

class TestIdempotencyKey:
    def test_key_format(self):
        worker = _make_worker(pipeline=MagicMock(spec=ProductionEvalPipeline))
        event = _make_event(job_id="job-abc", analysis_id="ana-xyz")
        key = worker._idempotency_key(event)
        assert key == "prod_eval:job-abc:ana-xyz"

    def test_different_jobs_have_different_keys(self):
        worker = _make_worker(pipeline=MagicMock(spec=ProductionEvalPipeline))
        e1 = _make_event(job_id="j1", analysis_id="a1")
        e2 = _make_event(job_id="j2", analysis_id="a2")
        assert worker._idempotency_key(e1) != worker._idempotency_key(e2)

    def test_same_job_different_analysis_has_different_key(self):
        worker = _make_worker(pipeline=MagicMock(spec=ProductionEvalPipeline))
        e1 = _make_event(job_id="j1", analysis_id="a1")
        e2 = _make_event(job_id="j1", analysis_id="a2")
        # Two refinement loops → two analyses → two eval runs
        assert worker._idempotency_key(e1) != worker._idempotency_key(e2)

    def test_non_eval_event_gets_event_id_key(self):
        worker = _make_worker(pipeline=MagicMock(spec=ProductionEvalPipeline))
        event_id = new_id()
        other_event = MagicMock()
        other_event.event_id = event_id
        # Does NOT isinstance check as EvalSampleEvent
        key = worker._idempotency_key(other_event)
        assert key == f"prod_eval:{event_id}"


# ---------------------------------------------------------------------------
# Handle dispatch
# ---------------------------------------------------------------------------

class TestHandleDispatch:
    @pytest.mark.asyncio
    async def test_dispatches_to_pipeline(self):
        eval_result = _make_eval_result()
        pipeline_result = ProductionEvalResult(
            analysis_id="a1",
            evaluation_result=eval_result,
        )
        pipeline = MagicMock(spec=ProductionEvalPipeline)
        pipeline.evaluate_analysis = AsyncMock(return_value=pipeline_result)

        worker = _make_worker(pipeline=pipeline)
        event = _make_event(job_id="j1", analysis_id="a1", tenant_id="t1")

        await worker.handle(event)

        pipeline.evaluate_analysis.assert_called_once_with(
            tenant_id="t1",
            analysis_id="a1",
            job_id="j1",
        )

    @pytest.mark.asyncio
    async def test_skipped_result_does_not_raise(self):
        skipped = ProductionEvalResult(
            analysis_id="a1",
            evaluation_result=_make_eval_result(),
            skipped=True,
            skip_reason="analysis_not_found",
        )
        pipeline = MagicMock(spec=ProductionEvalPipeline)
        pipeline.evaluate_analysis = AsyncMock(return_value=skipped)

        worker = _make_worker(pipeline=pipeline)
        event = _make_event()

        # Should complete without raising
        await worker.handle(event)

    @pytest.mark.asyncio
    async def test_unexpected_event_type_discarded(self):
        pipeline = MagicMock(spec=ProductionEvalPipeline)
        pipeline.evaluate_analysis = AsyncMock()

        worker = _make_worker(pipeline=pipeline)

        # Use a non-EvalSampleEvent
        other_event = MagicMock()
        other_event.event_type = "analysis.completed"

        await worker.handle(other_event)

        # Pipeline should NOT have been called
        pipeline.evaluate_analysis.assert_not_called()


# ---------------------------------------------------------------------------
# EvalSampleEvent sampling rate (analyst worker)
# ---------------------------------------------------------------------------

class TestSamplingDeterminism:
    """hash(job_id) % 100 == 0 must be consistent for the same job_id."""

    def test_sampling_is_deterministic(self):
        """Same job_id always produces the same sampling decision."""
        job_id = "test-job-12345"
        decision_1 = hash(job_id) % 100 == 0
        decision_2 = hash(job_id) % 100 == 0
        assert decision_1 == decision_2

    def test_approximately_one_percent(self):
        """Verify sampling distribution is roughly 1% over a large range."""
        sample_count = sum(
            1 for i in range(10_000) if hash(f"job-{i}") % 100 == 0
        )
        # Expect ~100 samples; allow ±50% tolerance for hash distribution variance
        assert 50 <= sample_count <= 150, f"Expected ~100 samples, got {sample_count}"

    def test_sampled_job_always_sampled(self):
        """A job that is sampled once is always sampled (idempotent decision)."""
        # Find a job_id that is sampled
        sampled_job = None
        for i in range(1000):
            jid = f"job-{i}"
            if hash(jid) % 100 == 0:
                sampled_job = jid
                break

        if sampled_job is None:
            pytest.skip("no sampled job found in 1000 tries")

        # Verify it's consistently sampled
        assert hash(sampled_job) % 100 == 0
        assert hash(sampled_job) % 100 == 0  # second call same result


# ---------------------------------------------------------------------------
# ProductionEvalPipeline — unit-level dispatch test
# ---------------------------------------------------------------------------

class TestProductionEvalPipeline:
    @pytest.mark.asyncio
    async def test_analysis_not_found_returns_skipped(self):
        analysis_repo = MagicMock()
        analysis_repo.get = AsyncMock(return_value=None)
        chunk_repo = MagicMock()
        runner = MagicMock()
        runner._evaluators = []

        pipeline = ProductionEvalPipeline(
            runner=runner,
            analysis_repo=analysis_repo,
            chunk_repo=chunk_repo,
        )
        result = await pipeline.evaluate_analysis(
            tenant_id="t1",
            analysis_id="missing-id",
            job_id="j1",
        )

        assert result.skipped is True
        assert result.skip_reason == "analysis_not_found"

    @pytest.mark.asyncio
    async def test_empty_insights_returns_skipped(self):
        from trendstorm.domain.analyses.models import Analysis

        analysis = Analysis(
            id="a1",
            tenant_id="t1",
            job_id="j1",
            category_id="cat",
            summary="summary",
            insights=[],
            citations=[],
        )
        analysis_repo = MagicMock()
        analysis_repo.get = AsyncMock(return_value=analysis)
        chunk_repo = MagicMock()
        runner = MagicMock()
        runner._evaluators = []

        pipeline = ProductionEvalPipeline(
            runner=runner,
            analysis_repo=analysis_repo,
            chunk_repo=chunk_repo,
        )
        result = await pipeline.evaluate_analysis(
            tenant_id="t1",
            analysis_id="a1",
            job_id="j1",
        )

        assert result.skipped is True
        assert result.skip_reason == "no_insights"
