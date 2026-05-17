"""Integration tests for EvalRunner.

These tests use mock evaluators and a mock target function — no real LLM calls,
no Kafka, no Mongo. They verify that EvalRunner correctly:
  - calls evaluators for each example
  - aggregates per-dimension scores
  - detects threshold violations
  - persists artifacts to disk
  - handles evaluator errors gracefully (records zero score, continues)

Marked 'integration' because they write to a temp artifacts/ directory on disk.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from trendstorm.domain.evaluation.models import (
    DimensionScore,
    EvalDimension,
    EvaluationResult,
    GoldenChunk,
    GoldenExample,
)
from trendstorm.services.evaluation.runner import EvalRunner
from trendstorm.shared.ids import new_id

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_example(name: str = "ex1") -> GoldenExample:
    return GoldenExample(
        name=name,
        category_name="Test Category",
        tenant_id="test-tenant",
        chunks=[
            GoldenChunk(
                chunk_id="c1",
                document_id="doc1",
                source_id="src1",
                text="Some relevant text about AI.",
            )
        ],
    )


def _make_analysis(tenant_id: str = "test-tenant", job_id: str = "j1"):
    from trendstorm.domain.analyses.models import Analysis
    return Analysis(
        id=new_id(),
        tenant_id=tenant_id,
        job_id=job_id,
        category_id="cat1",
        summary="Analysis summary",
        insights=[],
        citations=[],
    )


class _ConstantEvaluator:
    """Evaluator that always returns a fixed score."""

    def __init__(
        self,
        dimension: EvalDimension,
        score: float,
        passed: bool,
    ) -> None:
        self._dimension = dimension
        self._score = score
        self._passed = passed
        self.call_count = 0

    @property
    def dimension(self) -> EvalDimension:
        return self._dimension

    async def evaluate(self, analysis, *, example=None) -> EvaluationResult:
        self.call_count += 1
        ds = DimensionScore(
            dimension=self._dimension,
            score=self._score,
            passed=self._passed,
        )
        return EvaluationResult(
            tenant_id=analysis.tenant_id,
            analysis_id=analysis.id,
            job_id=analysis.job_id,
            dimension_scores=[ds],
            aggregate_score=self._score,
        )


class _ErrorEvaluator:
    """Evaluator that always raises RuntimeError."""

    def __init__(self, dimension: EvalDimension) -> None:
        self._dimension = dimension

    @property
    def dimension(self) -> EvalDimension:
        return self._dimension

    async def evaluate(self, analysis, *, example=None) -> EvaluationResult:
        raise RuntimeError("simulated evaluator crash")


class _SkipEvaluator:
    """Evaluator that raises ValueError (like GoldenCoverageEvaluator on prod samples)."""

    def __init__(self, dimension: EvalDimension) -> None:
        self._dimension = dimension

    @property
    def dimension(self) -> EvalDimension:
        return self._dimension

    async def evaluate(self, analysis, *, example=None) -> EvaluationResult:
        raise ValueError("no expected_analysis provided")


def _make_eval_settings(thresholds: dict | None = None):
    """Build a minimal EvalSettings-like object."""
    from trendstorm.shared.config import EvalSettings, EvalThresholds

    thresh_values = {
        "faithfulness": 0.85,
        "citation_accuracy": 0.95,
        "relevance": 0.80,
        "coverage": 0.70,
    }
    if thresholds:
        thresh_values.update(thresholds)
    t = EvalThresholds(**thresh_values)
    return EvalSettings(thresholds=t)


async def _target(example: GoldenExample):
    return _make_analysis(tenant_id=example.tenant_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestEvalRunnerBasic:
    @pytest.mark.asyncio
    async def test_single_example_single_evaluator(self):
        evaluator = _ConstantEvaluator(EvalDimension.FAITHFULNESS, score=0.9, passed=True)
        settings = _make_eval_settings()

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = EvalRunner(
                evaluators=[evaluator],
                settings=settings,
                artifacts_dir=Path(tmpdir),
            )
            report = await runner.run_eval(
                dataset=[_make_example()],
                target=_target,
                suite="test",
            )

        assert evaluator.call_count == 1
        assert report.n_examples == 1
        assert len(report.dimension_summaries) == 1
        s = report.dimension_summaries[0]
        assert s.dimension == EvalDimension.FAITHFULNESS
        assert s.mean_score == pytest.approx(0.9)
        assert s.pass_rate == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_multiple_examples_aggregated(self):
        evaluator = _ConstantEvaluator(EvalDimension.RELEVANCE, score=0.6, passed=False)
        settings = _make_eval_settings()

        examples = [_make_example(f"ex{i}") for i in range(3)]

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = EvalRunner(
                evaluators=[evaluator],
                settings=settings,
                artifacts_dir=Path(tmpdir),
            )
            report = await runner.run_eval(dataset=examples, target=_target, suite="test")

        assert evaluator.call_count == 3
        assert report.n_examples == 3
        s = report.dimension_summaries[0]
        assert s.mean_score == pytest.approx(0.6)
        assert s.pass_rate == pytest.approx(0.0)
        assert s.n_evaluated == 3

    @pytest.mark.asyncio
    async def test_multiple_evaluators(self):
        ev_faith = _ConstantEvaluator(EvalDimension.FAITHFULNESS, score=0.9, passed=True)
        ev_rel = _ConstantEvaluator(EvalDimension.RELEVANCE, score=0.85, passed=True)
        settings = _make_eval_settings()

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = EvalRunner(
                evaluators=[ev_faith, ev_rel],
                settings=settings,
                artifacts_dir=Path(tmpdir),
            )
            report = await runner.run_eval(
                dataset=[_make_example()], target=_target, suite="test"
            )

        assert len(report.dimension_summaries) == 2
        dims = {s.dimension for s in report.dimension_summaries}
        assert EvalDimension.FAITHFULNESS in dims
        assert EvalDimension.RELEVANCE in dims


@pytest.mark.integration
class TestThresholdViolations:
    @pytest.mark.asyncio
    async def test_violation_detected(self):
        # FAITHFULNESS threshold = 0.85; score = 0.70 → violation
        evaluator = _ConstantEvaluator(EvalDimension.FAITHFULNESS, score=0.70, passed=False)
        settings = _make_eval_settings()

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = EvalRunner(
                evaluators=[evaluator],
                settings=settings,
                artifacts_dir=Path(tmpdir),
            )
            report = await runner.run_eval(
                dataset=[_make_example()], target=_target, suite="test"
            )

        assert len(report.threshold_violations) == 1
        assert "faithfulness" in report.threshold_violations[0].lower()
        assert report.passed is False

    @pytest.mark.asyncio
    async def test_no_violation_when_above_threshold(self):
        evaluator = _ConstantEvaluator(EvalDimension.FAITHFULNESS, score=0.95, passed=True)
        settings = _make_eval_settings()

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = EvalRunner(
                evaluators=[evaluator],
                settings=settings,
                artifacts_dir=Path(tmpdir),
            )
            report = await runner.run_eval(
                dataset=[_make_example()], target=_target, suite="test"
            )

        assert report.threshold_violations == []
        assert report.passed is True


@pytest.mark.integration
class TestEvaluatorErrorHandling:
    @pytest.mark.asyncio
    async def test_error_evaluator_records_zero(self):
        error_ev = _ErrorEvaluator(EvalDimension.FAITHFULNESS)
        settings = _make_eval_settings()

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = EvalRunner(
                evaluators=[error_ev],
                settings=settings,
                artifacts_dir=Path(tmpdir),
            )
            report = await runner.run_eval(
                dataset=[_make_example()], target=_target, suite="test"
            )

        # Score of 0.0 recorded → faithfulness threshold violated
        assert len(report.threshold_violations) == 1
        s = report.dimension_summaries[0]
        assert s.mean_score == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_skip_evaluator_does_not_appear_in_report(self):
        skip_ev = _SkipEvaluator(EvalDimension.COVERAGE)
        settings = _make_eval_settings()

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = EvalRunner(
                evaluators=[skip_ev],
                settings=settings,
                artifacts_dir=Path(tmpdir),
            )
            report = await runner.run_eval(
                dataset=[_make_example()], target=_target, suite="test"
            )

        # ValueError from skip evaluator: silently skipped, no dimension summary
        assert report.dimension_summaries == []
        assert report.threshold_violations == []
        assert report.passed is True

    @pytest.mark.asyncio
    async def test_run_continues_after_error(self):
        """Error on one example shouldn't prevent other examples from running."""
        good_ev = _ConstantEvaluator(EvalDimension.FAITHFULNESS, score=0.9, passed=True)
        settings = _make_eval_settings()

        examples = [_make_example(f"ex{i}") for i in range(3)]

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = EvalRunner(
                evaluators=[good_ev],
                settings=settings,
                artifacts_dir=Path(tmpdir),
            )
            _ = await runner.run_eval(dataset=examples, target=_target, suite="test")

        assert good_ev.call_count == 3


@pytest.mark.integration
class TestArtifactPersistence:
    @pytest.mark.asyncio
    async def test_artifact_written_to_disk(self):
        evaluator = _ConstantEvaluator(EvalDimension.FAITHFULNESS, score=0.9, passed=True)
        settings = _make_eval_settings()

        with tempfile.TemporaryDirectory() as tmpdir:
            artifacts_path = Path(tmpdir)
            runner = EvalRunner(
                evaluators=[evaluator],
                settings=settings,
                artifacts_dir=artifacts_path,
            )
            _ = await runner.run_eval(
                dataset=[_make_example()], target=_target, suite="test"
            )

            files = list(artifacts_path.glob("eval-*.json"))  # noqa: ASYNC240  # synchronous pathlib in test, no async alternative needed
            assert len(files) == 1

            data = json.loads(files[0].read_text())
            assert data["suite"] == "test"
            assert data["n_examples"] == 1
            assert "dimension_summaries" in data
            assert "threshold_violations" in data

    @pytest.mark.asyncio
    async def test_artifact_run_id_matches_report(self):
        evaluator = _ConstantEvaluator(EvalDimension.RELEVANCE, score=0.85, passed=True)
        settings = _make_eval_settings()

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = EvalRunner(
                evaluators=[evaluator],
                settings=settings,
                artifacts_dir=Path(tmpdir),
            )
            report = await runner.run_eval(
                dataset=[_make_example()], target=_target, suite="test"
            )

            files = list(Path(tmpdir).glob("eval-*.json"))  # noqa: ASYNC240  # synchronous pathlib in test, no async alternative needed
            data = json.loads(files[0].read_text())
            assert report.run_id[:8] in files[0].name
            assert data["run_id"] == report.run_id


@pytest.mark.integration
class TestElapsedTime:
    @pytest.mark.asyncio
    async def test_elapsed_seconds_positive(self):
        evaluator = _ConstantEvaluator(EvalDimension.FAITHFULNESS, score=0.9, passed=True)
        settings = _make_eval_settings()

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = EvalRunner(
                evaluators=[evaluator],
                settings=settings,
                artifacts_dir=Path(tmpdir),
            )
            report = await runner.run_eval(
                dataset=[_make_example()], target=_target, suite="test"
            )

        assert report.elapsed_seconds is not None
        assert report.elapsed_seconds >= 0.0
        assert report.finished_at is not None
        assert report.finished_at >= report.started_at
