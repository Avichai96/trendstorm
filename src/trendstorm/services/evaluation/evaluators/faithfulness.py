"""LLMPanelFaithfulnessEvaluator — multi-judge faithfulness scoring.

Scores each Insight in the Analysis independently then aggregates:
    per_insight_score = panel.vote(claim, chunk_evidence).score
    analysis_score    = mean(per_insight_scores)

Why per-insight?
    A single panel call on the entire analysis cannot attribute failures
    to specific insights. Per-insight scoring gives the grounding dimension
    of the eval report fine-grained signal — "insight 2 of 5 was hallucinated"
    rather than "faithfulness failed."

Evidence construction:
    For each insight, concatenate the text of all chunks referenced in
    supporting_chunk_ids. These chunk texts come from the GoldenExample
    if an example is provided, otherwise from the retrieved_chunks kwarg
    (callers must provide one or the other).
"""
from __future__ import annotations

import importlib.resources
from typing import TYPE_CHECKING

from opentelemetry import trace

from trendstorm.domain.evaluation.models import DimensionScore, EvalDimension, EvaluationResult
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.domain.analyses.models import Analysis
    from trendstorm.domain.evaluation.models import GoldenExample
    from trendstorm.services.evaluation.panel import LLMPanel

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

_TOOL_NAME = "record_faithfulness_vote"
_TOOL_SCHEMA = {
    "name": _TOOL_NAME,
    "description": "Record your faithfulness vote for the claim.",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {"type": "number", "description": "Faithfulness score 0.0-1.0"},
            "passed": {"type": "boolean"},
            "rationale": {"type": "string"},
        },
        "required": ["score", "passed", "rationale"],
    },
}


def _load_prompt() -> str:
    pkg = importlib.resources.files("trendstorm.services.evaluation.prompts")
    return (pkg / "faithfulness_judge.md").read_text(encoding="utf-8").strip()


class LLMPanelFaithfulnessEvaluator:
    """Faithfulness evaluator backed by an LLMPanel.

    Satisfies the Evaluator Protocol structurally.

    Args:
        panel:        LLMPanel configured with judge models.
        _prompt_text: override prompt for unit tests; None loads from file.

    """

    def __init__(
        self,
        panel: LLMPanel,
        *,
        _prompt_text: str | None = None,
    ) -> None:
        self._panel = panel
        self._prompt = _prompt_text if _prompt_text is not None else _load_prompt()

    @property
    def dimension(self) -> EvalDimension:
        return EvalDimension.FAITHFULNESS

    async def evaluate(
        self,
        analysis: Analysis,
        *,
        example: GoldenExample | None = None,
    ) -> EvaluationResult:
        chunk_index = _build_chunk_index(example)

        with tracer.start_as_current_span("eval.faithfulness") as span:
            span.set_attribute("eval.n_insights", len(analysis.insights))

            if not analysis.insights:
                score = 1.0
                return _build_result(analysis, score, passed=True, rationale="no insights to evaluate")

            per_insight_scores: list[float] = []

            for insight in analysis.insights:
                evidence = _build_evidence(insight.supporting_chunk_ids, chunk_index)
                if not evidence:
                    logger.debug(
                        "eval.faithfulness.no_evidence",
                        claim=insight.claim[:80],
                    )
                    per_insight_scores.append(0.0)
                    continue

                try:
                    result = await self._panel.vote(
                        claim=insight.claim,
                        evidence=evidence,
                        context={
                            "system_prompt": self._prompt,
                            "tool_schema": _TOOL_SCHEMA,
                            "tool_name": _TOOL_NAME,
                        },
                    )
                    per_insight_scores.append(result.score)
                except Exception as exc:
                    logger.warning(
                        "eval.faithfulness.panel_error",
                        claim=insight.claim[:80],
                        error=str(exc),
                    )
                    per_insight_scores.append(0.0)

            score = sum(per_insight_scores) / len(per_insight_scores) if per_insight_scores else 0.0
            score = round(score, 6)
            passed = score >= 0.75

            span.set_attribute("eval.faithfulness.score", score)
            return _build_result(analysis, score, passed=passed)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _build_chunk_index(example: GoldenExample | None) -> dict[str, str]:
    """Build chunk_id → text mapping from GoldenExample chunks, if provided."""
    if example is None:
        return {}
    return {c.chunk_id: c.text for c in example.chunks}


def _build_evidence(chunk_ids: list[str], chunk_index: dict[str, str]) -> str:
    """Concatenate chunk texts for the given IDs. Skips missing IDs."""
    parts = [chunk_index[cid] for cid in chunk_ids if cid in chunk_index]
    return "\n\n".join(parts)


def _build_result(
    analysis: Analysis,
    score: float,
    *,
    passed: bool,
    rationale: str = "",
) -> EvaluationResult:
    ds = DimensionScore(
        dimension=EvalDimension.FAITHFULNESS,
        score=score,
        passed=passed,
        rationale=rationale,
    )
    return EvaluationResult(
        tenant_id=analysis.tenant_id,
        analysis_id=analysis.id,
        job_id=analysis.job_id,
        dimension_scores=[ds],
        aggregate_score=score,
    )
