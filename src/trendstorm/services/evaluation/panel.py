"""LLMPanel — concurrent multi-judge scoring with aggregation.

Calls all judges concurrently via asyncio.gather(return_exceptions=True) so
one slow or failing judge doesn't block the panel. Valid votes are collected,
checked against min_quorum, then aggregated via the chosen PanelAggregation.

Aggregation strategies:
    MEAN     — arithmetic mean of scores; majority vote for passed.
    MEDIAN   — median of scores; majority vote for passed.
    MIN      — minimum score; all judges must pass for passed=True.
    MAJORITY — mean score; majority (>50%) judges must pass for passed=True.

Why asyncio.gather over sequential calls?
    Panel latency ≈ max(judge latency) instead of sum(). At 3 judges and ~2s
    each, the panel wall-clock time drops from ~6s to ~2s. Failures are still
    isolated per judge via return_exceptions=True.
"""
from __future__ import annotations

import asyncio
import statistics
from typing import Any

from opentelemetry import trace

from trendstorm.domain.evaluation.judge import (
    JudgeVote,
    LLMJudge,
    PanelAggregation,
    PanelInsufficientVotesError,
    PanelResult,
)
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


def _aggregate_votes(
    votes: list[JudgeVote],
    aggregation: PanelAggregation,
) -> PanelResult:
    """Aggregate a list of judge votes into a PanelResult.

    Raises PanelInsufficientVotesError if votes is empty.
    """
    if not votes:
        raise PanelInsufficientVotesError(received=0, required=1)

    scores = [v.score for v in votes]
    passes = [v.passed for v in votes]

    if aggregation == PanelAggregation.MEAN:
        score = sum(scores) / len(scores)
        passed = sum(passes) > len(passes) / 2

    elif aggregation == PanelAggregation.MEDIAN:
        score = statistics.median(scores)
        passed = sum(passes) > len(passes) / 2

    elif aggregation == PanelAggregation.MIN:
        score = min(scores)
        passed = all(passes)

    elif aggregation == PanelAggregation.MAJORITY:
        score = sum(scores) / len(scores)
        passed = sum(passes) > len(passes) / 2

    else:
        score = sum(scores) / len(scores)
        passed = sum(passes) > len(passes) / 2

    return PanelResult(
        votes=votes,
        score=round(score, 6),
        passed=passed,
        aggregation=aggregation,
        n_judges=len(votes),
        n_valid=len(votes),
    )


class LLMPanel:
    """Orchestrates a fixed set of LLMJudge implementations.

    Args:
        judges:      ordered list of judges; at least 1 required.
        settings:    EvalSettings — provides min_quorum and aggregation.
        aggregation: override for aggregation strategy (default: MEAN).
        min_quorum:  override for minimum quorum (default: from settings or 2).

    """

    def __init__(
        self,
        judges: list[LLMJudge],
        settings: Any = None,
        *,
        aggregation: PanelAggregation = PanelAggregation.MEAN,
        min_quorum: int | None = None,
    ) -> None:
        if not judges:
            raise ValueError("LLMPanel requires at least one judge")
        self._judges = judges
        self._settings = settings
        # settings overrides defaults; explicit kwargs override settings.
        _default_quorum = getattr(settings, "min_quorum", 2) if settings else 2
        self._min_quorum = min_quorum if min_quorum is not None else _default_quorum
        if self._min_quorum < 1:
            raise ValueError("min_quorum must be >= 1")
        self._aggregation = aggregation

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def vote(
        self,
        *,
        claim: str,
        evidence: str,
        context: dict[str, Any] | None = None,
    ) -> PanelResult:
        """Call all judges concurrently and aggregate their votes.

        Args:
            claim:    the assertion being judged.
            evidence: supporting text (chunk concatenation, etc.).
            context:  optional dimension-specific metadata forwarded to each judge.

        Returns:
            PanelResult with aggregated score and passed judgment.

        Raises:
            PanelInsufficientVotesError if fewer than min_quorum judges succeed.

        """
        ctx = context or {}
        with tracer.start_as_current_span("eval.panel.vote") as span:
            span.set_attribute("eval.panel.n_judges", len(self._judges))
            span.set_attribute("eval.panel.aggregation", self._aggregation)

            results = await asyncio.gather(
                *[j.judge(claim=claim, evidence=evidence, context=ctx) for j in self._judges],
                return_exceptions=True,
            )

            votes: list[JudgeVote] = []
            errors: list[str] = []
            for judge, item in zip(self._judges, results, strict=False):
                if isinstance(item, BaseException):
                    logger.warning(
                        "eval.judge_failed",
                        judge_model=judge.model_id,
                        error=str(item),
                    )
                    errors.append(f"{judge.model_id}: {item}")
                else:
                    votes.append(item)

            n_valid = len(votes)
            span.set_attribute("eval.panel.n_valid", n_valid)

            if n_valid < self._min_quorum:
                raise PanelInsufficientVotesError(
                    required=self._min_quorum,
                    received=n_valid,
                    errors=errors,
                )

            aggregated = _aggregate_votes(votes, self._aggregation)
            span.set_attribute("eval.panel.score", aggregated.score)
            span.set_attribute("eval.panel.passed", aggregated.passed)

            return PanelResult(
                votes=votes,
                score=aggregated.score,
                passed=aggregated.passed,
                aggregation=self._aggregation,
                n_judges=len(self._judges),
                n_valid=n_valid,
            )

