"""LLMPanelRelevanceEvaluator — single panel call per analysis.

Unlike faithfulness (per-insight), relevance is assessed at the analysis level
via the summary. One panel.vote() call with the full summary as the claim and
the category brief as the evidence/context.

Why single call?
    Relevance is a global property of the analysis — it cannot be decomposed
    into per-insight assessments because an individual insight may be on-topic
    while the overall analysis is not, or vice versa. The judges are given the
    summary and category brief and asked to assess overall alignment.
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

_TOOL_NAME = "record_relevance_vote"
_TOOL_SCHEMA = {
    "name": _TOOL_NAME,
    "description": "Record your relevance vote for the analysis.",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {"type": "number", "description": "Relevance score 0.0-1.0"},
            "passed": {"type": "boolean"},
            "rationale": {"type": "string"},
        },
        "required": ["score", "passed", "rationale"],
    },
}


def _load_prompt() -> str:
    pkg = importlib.resources.files("trendstorm.services.evaluation.prompts")
    return (pkg / "relevance_judge.md").read_text(encoding="utf-8").strip()


class LLMPanelRelevanceEvaluator:
    """Relevance evaluator backed by an LLMPanel.

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
        return EvalDimension.RELEVANCE

    async def evaluate(
        self,
        analysis: Analysis,
        *,
        example: GoldenExample | None = None,
    ) -> EvaluationResult:
        with tracer.start_as_current_span("eval.relevance") as span:
            category_brief = _format_category_context(
                category_name=analysis.category_id,  # id is all we have on Analysis
                example=example,
            )

            try:
                result = await self._panel.vote(
                    claim=analysis.summary,
                    evidence=category_brief,
                    context={
                        "system_prompt": self._prompt,
                        "tool_schema": _TOOL_SCHEMA,
                        "tool_name": _TOOL_NAME,
                    },
                )
                score = result.score
                passed = result.passed
            except Exception as exc:
                logger.warning("eval.relevance.panel_error", error=str(exc))
                score = 0.0
                passed = False

            span.set_attribute("eval.relevance.score", score)
            return _build_result(analysis, score, passed=passed)


def _format_category_context(
    category_name: str,
    example: GoldenExample | None,
) -> str:
    if example is None:
        return f"Category: {category_name}"
    parts = [f"Category: {example.category_name}"]
    if example.category_description:
        parts.append(example.category_description)
    if example.category_keywords:
        parts.append(f"Keywords: {', '.join(example.category_keywords)}")
    return "\n".join(parts)


def _build_result(
    analysis: Analysis,
    score: float,
    *,
    passed: bool,
) -> EvaluationResult:
    ds = DimensionScore(
        dimension=EvalDimension.RELEVANCE,
        score=score,
        passed=passed,
    )
    return EvaluationResult(
        tenant_id=analysis.tenant_id,
        analysis_id=analysis.id,
        job_id=analysis.job_id,
        dimension_scores=[ds],
        aggregate_score=score,
    )
