"""AnalysisValidator — independent rubric-based scoring of analyst output.

Design:
    - SEPARATE LLM call from the analyst. Different system prompt, designed to
      surface failure modes the analyst would not catch.
    - Structured output via tool-use (record_validation tool). Schema validity
      is guaranteed by the provider — no prose JSON parsing.
    - Returns a ValidationResult value object. The analyst service combines
      this with the threshold from AnalysisSettings to decide pass/fail at the
      orchestration level.
    - Two pass signals: the LLM's `passed` judgment AND `score >= threshold`.
      Callers can use either or both; the orchestrator uses the threshold check
      as the canonical pass signal so the threshold is a tunable knob.

Prompt:
    Loaded from services/analysis/prompts/validator_system.md via importlib.
    Never inline string literals — prompts are content, not code.

Tool name:
    "record_validation" — this exact name is referenced in validator_system.md
    and pinned by the prompt smoke tests. Do not rename without updating the
    prompt and tests.
"""
from __future__ import annotations

import importlib.resources
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field

from trendstorm.domain.llm.models import Message
from trendstorm.shared.errors import LLMSchemaError
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.domain.analyses.models import Analysis
    from trendstorm.domain.categories.models import Category
    from trendstorm.domain.llm.providers import StructuredChatProvider
    from trendstorm.domain.retrieval.models import RetrievedChunk
    from trendstorm.shared.config import AnalysisSettings

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

_VALIDATOR_TOOL_NAME = "record_validation"

# JSON schema for the record_validation tool. Pinned schema = predictable shape.
_VALIDATOR_TOOL_SCHEMA: dict[str, Any] = {
    "name": _VALIDATOR_TOOL_NAME,
    "description": (
        "Record the validation score, pass/fail judgment, and actionable "
        "feedback for an analyst's output."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {
                "type": "number",
                "description": "Weighted aggregate score from the rubric (0.0-1.0).",
            },
            "passed": {
                "type": "boolean",
                "description": "True if the analysis meets the publishable-quality bar.",
            },
            "notes": {
                "type": "string",
                "description": (
                    "Concrete, actionable feedback. Name insights by their claim text, "
                    "reference specific chunk_ids, point at exact problems."
                ),
            },
        },
        "required": ["score", "passed", "notes"],
    },
}


def _load_validator_prompt() -> str:
    pkg = importlib.resources.files("trendstorm.services.analysis.prompts")
    return (pkg / "validator_system.md").read_text(encoding="utf-8").strip()


class ValidationResult(BaseModel):
    """Output of one validator pass.

    The LLM-provided `passed` field is its own subjective judgment; the
    orchestrator compares `score` against AnalysisSettings.validator_threshold
    as the canonical pass signal. Both are exposed so callers can use either.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    score: float = Field(..., ge=0.0, le=1.0)
    passed: bool
    notes: str = Field(default="", max_length=5000)

    def meets_threshold(self, threshold: float) -> bool:
        return self.score >= threshold


class AnalysisValidator:
    """Validates an Analysis using a separate LLM call against a rubric.

    Args:
        chat_provider   — StructuredChatProvider (Anthropic, Gemini, or OpenAI).
        settings        — AnalysisSettings (for default threshold).
        _prompt_text    — Override the prompt for unit tests; None loads the file.

    """

    def __init__(
        self,
        chat_provider: StructuredChatProvider,
        settings: AnalysisSettings,
        *,
        _prompt_text: str | None = None,
    ) -> None:
        self._chat = chat_provider
        self._settings = settings
        self._prompt: str = (
            _prompt_text if _prompt_text is not None else _load_validator_prompt()
        )

    async def validate(
        self,
        analysis: Analysis,
        retrieved_chunks: list[RetrievedChunk],
        category: Category | None = None,
    ) -> ValidationResult:
        """Score the analysis against the rubric. Returns the structured result."""
        with tracer.start_as_current_span("analysis.validate") as span:
            span.set_attribute("validator.model_id", self._chat.model_id)
            span.set_attribute("validator.n_insights", len(analysis.insights))
            span.set_attribute("validator.n_chunks", len(retrieved_chunks))

            messages = [
                Message(role="system", content=self._prompt),
                Message(
                    role="user",
                    content=_format_user_message(analysis, retrieved_chunks, category),
                ),
            ]

            try:
                name, args, _token_usage = await self._chat.complete_with_tools(
                    messages,
                    tools=[_VALIDATOR_TOOL_SCHEMA],
                    tool_choice=_VALIDATOR_TOOL_NAME,
                )
            except LLMSchemaError:
                # Surface up unchanged — the orchestrator decides whether to
                # retry the validator or treat as a low-confidence pass.
                raise

            if name != _VALIDATOR_TOOL_NAME:
                raise LLMSchemaError(
                    f"Validator returned unexpected tool: {name!r}",
                    context={"expected": _VALIDATOR_TOOL_NAME, "received": name},
                )

            result = _parse_validation_args(args)
            span.set_attribute("validator.score", result.score)
            span.set_attribute("validator.passed", result.passed)
            logger.info(
                "validator_complete",
                score=result.score,
                passed=result.passed,
                threshold=self._settings.validator_threshold,
                meets_threshold=result.meets_threshold(self._settings.validator_threshold),
            )
            return result


def _parse_validation_args(args: dict[str, Any]) -> ValidationResult:
    """Convert the tool-use args dict into a ValidationResult.

    Coerces numbers that arrive as int (LLMs sometimes return 1 instead of 1.0)
    and clamps the score to [0, 1] defensively.
    """
    try:
        score = float(args["score"])
    except (KeyError, TypeError, ValueError) as exc:
        raise LLMSchemaError(
            "Validator tool output missing or non-numeric 'score'",
            context={"args": args, "error": str(exc)},
        ) from exc

    score = max(0.0, min(1.0, score))
    passed = bool(args.get("passed", False))
    notes = str(args.get("notes", ""))
    return ValidationResult(score=score, passed=passed, notes=notes)


def _format_user_message(
    analysis: Analysis,
    chunks: list[RetrievedChunk],
    category: Category | None,
) -> str:
    """Render the inputs into a single user message the validator can score.

    Format is structured Markdown — unambiguous for the LLM, debuggable for humans.
    """
    parts: list[str] = []

    # Category brief
    if category is not None:
        parts.append("## Category Brief")
        parts.append(f"**{category.name}**")
        if getattr(category, "description", None):
            parts.append(category.description or "")
        if getattr(category, "keywords", None):
            parts.append(f"Keywords: {', '.join(category.keywords)}")
        parts.append("")

    # Evidence corpus
    parts.append("## Evidence Corpus")
    if not chunks:
        parts.append("_(no chunks provided)_")
    for chunk in chunks:
        parts.append(f"### chunk_id: {chunk.chunk_id}")
        parts.append(f"document_id: {chunk.document_id}  source_id: {chunk.source_id}")
        parts.append(chunk.text)
        if chunk.parent_text:
            parts.append("")
            parts.append(f"*Parent context:* {chunk.parent_text}")
        parts.append("")

    # Analyst output
    parts.append("## Analyst Output")
    parts.append("### Summary")
    parts.append(analysis.summary)
    parts.append("")

    parts.append("### Insights")
    for i, insight in enumerate(analysis.insights, start=1):
        parts.append(f"**Insight {i}** [confidence: {insight.confidence:.2f}]")
        parts.append(f"Claim: {insight.claim}")
        if insight.rationale:
            parts.append(f"Rationale: {insight.rationale}")
        if insight.supporting_chunk_ids:
            parts.append(f"Supporting chunks: {', '.join(insight.supporting_chunk_ids)}")
        if insight.tags:
            parts.append(f"Tags: {', '.join(insight.tags)}")
        parts.append("")

    parts.append("### Citations")
    if not analysis.citations:
        parts.append("_(no citations)_")
    for cite in analysis.citations:
        url_suffix = f" — {cite.url}" if cite.url else ""
        parts.append(
            f"- **{cite.chunk_id}** (doc: {cite.document_id}, source: {cite.source_id}){url_suffix}"
        )
        parts.append(f'  Excerpt: "{cite.excerpt}"')

    return "\n".join(parts)
