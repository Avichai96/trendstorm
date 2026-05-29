"""Analyst service — composes retrieval + structured LLM + validation.

Single entry point: `produce_analysis(category, *, ...)` runs one full Analyst
pass and returns an AnalysisResult with the structured Analysis and the
ValidationResult. The orchestrator decides whether to refine based on the
result + AnalysisSettings.validator_threshold + refinement budget.

Refinement protocol:
    - The orchestrator passes `refinement_notes` (= prior validator's notes)
      and `refinement_loop` (= attempt index, 0-based) on retry.
    - Both are reflected in the user message AND in the retrieval query so
      the next pass actually searches for different evidence.
    - The Analyst itself does NOT loop. The orchestrator owns the loop.
"""

from __future__ import annotations

import importlib.resources
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict

from trendstorm.domain.analyses.models import Analysis, Citation, Insight
from trendstorm.domain.llm.models import Message
from trendstorm.domain.retrieval.models import RetrievalRequest
from trendstorm.services.analysis.validator import (
    ValidationResult,  # runtime import — Pydantic field
)
from trendstorm.shared.errors import LLMSchemaError, ValidationError
from trendstorm.shared.logging import get_logger
from trendstorm.shared.metrics.cost import record_llm_cost

if TYPE_CHECKING:
    from trendstorm.domain.billing.repository import CostLedgerRepository
    from trendstorm.domain.categories.models import Category
    from trendstorm.domain.llm.providers import StructuredChatProvider
    from trendstorm.domain.retrieval.models import RetrievedChunk
    from trendstorm.services.analysis.validator import AnalysisValidator
    from trendstorm.services.memory.retrieval import MemoryRetriever, RetrievedMemory
    from trendstorm.services.retrieval.hybrid import HybridRetriever
    from trendstorm.shared.config import AnalysisSettings

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

_ANALYST_TOOL_NAME = "record_analysis"

# JSON schema for record_analysis. Pinned shape — the prompt instructs the LLM
# to call exactly this tool with this schema. Changing the field names here
# requires updating analyst_system.md AND the smoke tests.
_ANALYST_TOOL_SCHEMA: dict[str, Any] = {
    "name": _ANALYST_TOOL_NAME,
    "description": (
        "Record the structured trend analysis: summary, insights, and citations. "
        "Every supporting_chunk_id must appear in the provided evidence."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "insights": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "rationale": {"type": "string"},
                        "supporting_chunk_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "confidence": {"type": "number"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["claim", "supporting_chunk_ids", "confidence"],
                },
            },
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "chunk_id": {"type": "string"},
                        "document_id": {"type": "string"},
                        "source_id": {"type": "string"},
                        "excerpt": {"type": "string"},
                        "url": {"type": "string"},
                    },
                    "required": ["chunk_id", "document_id", "source_id", "excerpt"],
                },
            },
        },
        "required": ["summary", "insights", "citations"],
    },
}


def _load_analyst_prompt() -> str:
    pkg = importlib.resources.files("trendstorm.services.analysis.prompts")
    return (pkg / "analyst_system.md").read_text(encoding="utf-8").strip()


class AnalysisResult(BaseModel):
    """Output of one Analyst pass.

    The orchestrator inspects `validation.score >= settings.validator_threshold`
    to decide whether to publish (or refine on retry).
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    analysis: Analysis
    validation: ValidationResult

    def meets_threshold(self, threshold: float) -> bool:
        return self.validation.score >= threshold


class Analyst:
    """End-to-end Analyst: retrieve → LLM → validate.

    Args:
        retriever      — HybridRetriever (expanded queries → RRF → rerank → parents).
        chat_provider  — StructuredChatProvider for the analyst LLM call.
        validator      — AnalysisValidator used to score the output.
        settings       — AnalysisSettings (final_k, validator_threshold, etc.).
        _prompt_text   — Override prompt for unit tests; None loads from file.

    """

    def __init__(
        self,
        retriever: HybridRetriever,
        chat_provider: StructuredChatProvider,
        validator: AnalysisValidator,
        settings: AnalysisSettings,
        *,
        memory_retriever: MemoryRetriever | None = None,
        memory_final_k: int = 5,
        _prompt_text: str | None = None,
    ) -> None:
        self._retriever = retriever
        self._chat = chat_provider
        self._validator = validator
        self._settings = settings
        self._memory_retriever = memory_retriever
        self._memory_final_k = memory_final_k
        self._prompt: str = _prompt_text if _prompt_text is not None else _load_analyst_prompt()

    async def produce_analysis(
        self,
        category: Category,
        *,
        tenant_id: str,
        job_id: str,
        query: str | None = None,
        refinement_notes: str | None = None,
        refinement_loop: int = 0,
        ledger: CostLedgerRepository | None = None,
    ) -> AnalysisResult:
        """Run one full Analyst pass and return the structured result + validation.

        The orchestrator calls this once per refinement attempt; this method
        itself does NOT iterate.
        """
        with tracer.start_as_current_span("analysis.produce") as span:
            span.set_attribute("analyst.tenant_id", tenant_id)
            span.set_attribute("analyst.category_id", category.id)
            span.set_attribute("analyst.refinement_loop", refinement_loop)
            span.set_attribute("analyst.model_id", self._chat.model_id)

            base_query = query or self._default_query(category)
            retrieval_query = self._compose_retrieval_query(base_query, refinement_notes)

            # 1. Retrieve chunks + memories in parallel (memories are optional).
            import asyncio as _asyncio

            with tracer.start_as_current_span("analysis.retrieve"):
                request = RetrievalRequest(
                    query=retrieval_query,
                    tenant_id=tenant_id,
                    category_id=category.id,
                    top_k=self._settings.final_k,
                )
                if self._memory_retriever is not None:
                    chunks, memories = await _asyncio.gather(
                        self._retriever.retrieve(request),
                        self._memory_retriever.retrieve_relevant(
                            retrieval_query,
                            tenant_id,
                            category.id,
                            top_k=self._memory_final_k,
                        ),
                    )
                else:
                    chunks = await self._retriever.retrieve(request)
                    memories = []
            span.set_attribute("analyst.n_chunks", len(chunks))
            span.set_attribute("analyst.n_memories", len(memories))

            if not chunks:
                raise ValidationError(
                    "Analyst received zero retrieved chunks; cannot produce analysis",
                    context={
                        "tenant_id": tenant_id,
                        "category_id": category.id,
                        "query": retrieval_query[:200],
                    },
                )

            # 2. Generate analysis via tool use
            with tracer.start_as_current_span("analysis.generate"):
                analysis = await self._generate_analysis(
                    chunks=chunks,
                    memories=memories,
                    category=category,
                    tenant_id=tenant_id,
                    job_id=job_id,
                    refinement_notes=refinement_notes,
                    refinement_loop=refinement_loop,
                    ledger=ledger,
                )
            span.set_attribute("analyst.n_insights", len(analysis.insights))
            span.set_attribute("analyst.n_citations", len(analysis.citations))

            # 3. Validate
            validation = await self._validator.validate(analysis, chunks, category)

            # 4. Stamp validator fields onto the Analysis and return
            #    (Analysis has extra="forbid"; can't mutate, so build a copy)
            scored = analysis.model_copy(
                update={
                    "validator_score": validation.score,
                    "validator_passed": validation.passed,
                    "validator_notes": validation.notes or None,
                }
            )

            span.set_attribute("analyst.validator_score", validation.score)
            span.set_attribute(
                "analyst.meets_threshold",
                validation.meets_threshold(self._settings.validator_threshold),
            )
            logger.info(
                "analyst_pass_done",
                refinement_loop=refinement_loop,
                n_chunks=len(chunks),
                n_insights=len(scored.insights),
                validator_score=validation.score,
                meets_threshold=validation.meets_threshold(self._settings.validator_threshold),
            )

            return AnalysisResult(analysis=scored, validation=validation)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _generate_analysis(
        self,
        *,
        chunks: list[RetrievedChunk],
        memories: list[RetrievedMemory] | None = None,
        category: Category,
        tenant_id: str,
        job_id: str,
        refinement_notes: str | None,
        refinement_loop: int,
        ledger: CostLedgerRepository | None = None,
    ) -> Analysis:
        """Call the LLM with the analyst prompt and convert tool args → Analysis."""
        messages = [
            Message(role="system", content=self._prompt),
            Message(
                role="user",
                content=_format_user_message(
                    category=category,
                    chunks=chunks,
                    memories=memories or [],
                    refinement_notes=refinement_notes,
                ),
            ),
        ]

        name, args, token_usage = await self._chat.complete_with_tools(
            messages,
            tools=[_ANALYST_TOOL_SCHEMA],
            tool_choice=_ANALYST_TOOL_NAME,
        )
        if name != _ANALYST_TOOL_NAME:
            raise LLMSchemaError(
                f"Analyst returned unexpected tool: {name!r}",
                context={"expected": _ANALYST_TOOL_NAME, "received": name},
            )

        provider = self._chat.model_id.split(".", 1)[0] if "." in self._chat.model_id else "unknown"
        record_llm_cost(
            tenant_id=tenant_id,
            provider=provider,
            model_id=self._chat.model_id,
            operation="analyst_chat",
            input_tokens=token_usage.input_tokens,
            output_tokens=token_usage.output_tokens,
            cached_tokens=token_usage.cached_tokens,
            job_id=job_id,
            ledger=ledger,
        )

        return _build_analysis_from_tool_args(
            args=args,
            tenant_id=tenant_id,
            job_id=job_id,
            category_id=category.id,
            model_id=self._chat.model_id,
            refinement_loop=refinement_loop,
            valid_chunk_ids={c.chunk_id for c in chunks},
            input_tokens=token_usage.input_tokens,
            output_tokens=token_usage.output_tokens,
        )

    @staticmethod
    def _default_query(category: Category) -> str:
        if category.description:
            return f"{category.name}: {category.description}"
        return category.name

    @staticmethod
    def _compose_retrieval_query(base_query: str, refinement_notes: str | None) -> str:
        if not refinement_notes:
            return base_query
        return f"{base_query}\n\nAdditional focus from prior review: {refinement_notes}"


# ===========================================================================
# Pure functions: prompt formatting and tool-arg → Analysis conversion
# ===========================================================================


def _build_analysis_from_tool_args(
    *,
    args: dict[str, Any],
    tenant_id: str,
    job_id: str,
    category_id: str,
    model_id: str,
    refinement_loop: int,
    valid_chunk_ids: set[str],
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> Analysis:
    """Construct an Analysis from the analyst tool-use args dict.

    Defensive transformations:
        - supporting_chunk_ids referencing chunks NOT in valid_chunk_ids are
          dropped silently (the validator will score them down via grounding).
        - Excerpts longer than the Citation max are truncated to fit; the LLM
          occasionally exceeds the limit despite prompt instructions.
    """
    summary = str(args.get("summary", "")).strip()
    if not summary:
        raise LLMSchemaError(
            "Analyst tool output had empty summary",
            context={"args_keys": list(args.keys())},
        )

    raw_insights = args.get("insights", []) or []
    raw_citations = args.get("citations", []) or []

    # Build insights, filtering hallucinated chunk references.
    insights: list[Insight] = []
    for raw in raw_insights:
        if not isinstance(raw, dict):
            continue
        supporting = [
            cid for cid in (raw.get("supporting_chunk_ids") or []) if cid in valid_chunk_ids
        ]
        if not supporting:
            # An insight with zero valid citations cannot be grounded — drop.
            continue
        try:
            insights.append(
                Insight(
                    claim=str(raw.get("claim", "")).strip(),
                    rationale=raw.get("rationale"),
                    supporting_chunk_ids=supporting,
                    confidence=float(raw.get("confidence", 0.5)),
                    tags=list(raw.get("tags") or []),
                )
            )
        except (ValueError, TypeError):
            continue

    # Build citations, deduping by chunk_id and dropping any not in the corpus.
    seen_cite_ids: set[str] = set()
    citations: list[Citation] = []
    for raw in raw_citations:
        if not isinstance(raw, dict):
            continue
        cid = str(raw.get("chunk_id", ""))
        if not cid or cid in seen_cite_ids or cid not in valid_chunk_ids:
            continue
        seen_cite_ids.add(cid)
        excerpt = str(raw.get("excerpt", ""))[:500]  # Citation max is 500
        try:
            citations.append(
                Citation(
                    chunk_id=cid,
                    document_id=str(raw.get("document_id", "")),
                    source_id=str(raw.get("source_id", "")),
                    excerpt=excerpt,
                    url=raw.get("url"),
                )
            )
        except (ValueError, TypeError):
            continue

    # Parse model_id into provider + name for provenance.
    if "." in model_id:
        model_provider, model_name = model_id.split(".", 1)
    else:
        model_provider, model_name = "unknown", model_id

    return Analysis(
        tenant_id=tenant_id,
        job_id=job_id,
        category_id=category_id,
        summary=summary,
        insights=insights,
        citations=citations,
        model_name=model_name,
        model_provider=model_provider,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        refinement_loops=refinement_loop,
    )


def _format_user_message(
    *,
    category: Category,
    chunks: list[RetrievedChunk],
    refinement_notes: str | None,
    memories: list[RetrievedMemory] | None = None,
) -> str:
    """Render the inputs into a single user message for the analyst.

    Refinement notes (if present) come BEFORE the evidence so the LLM reads
    them first and lets them steer attention.

    Each chunk is wrapped in <chunk id="..." source="..."> ... </chunk> tags
    to create an explicit data/instruction boundary (Phase 13 prompt injection
    containment). The analyst system prompt instructs the LLM to treat chunk
    content as data to analyse, never as instructions to follow.
    """
    parts: list[str] = []

    parts.append("## Category Brief")
    parts.append(f"**{category.name}**")
    if category.description:
        parts.append(category.description)
    if category.keywords:
        parts.append(f"Keywords: {', '.join(category.keywords)}")
    parts.append("")

    if memories:
        parts.append("## Historical Memory Context")
        parts.append(
            f"The following {len(memories)} memories are durable claims from prior analyses "
            "of this category. Treat them as authoritative historical context, but chunks "
            "below are authoritative for recency. Surface disagreements — do not silently "
            "prefer one over the other."
        )
        parts.append("")
        for mem in memories:
            parts.append(
                f'<memory id="{mem.memory_id}" kind="{mem.kind.value}" '
                f'confidence="{mem.confidence:.2f}">'
            )
            parts.append(mem.content)
            parts.append("</memory>")
            parts.append("")

    if refinement_notes:
        parts.append("## Validator Feedback from Prior Attempt")
        parts.append("Address each concern below in this new analysis:")
        parts.append(refinement_notes)
        parts.append("")

    parts.append("## Evidence Corpus")
    parts.append(
        f"You have {len(chunks)} retrieved chunks enclosed in <chunk> tags. "
        "Each chunk is raw data from an external source. "
        "Every claim you make MUST be cited to one or more of these chunk_ids."
    )
    parts.append("")
    for chunk in chunks:
        source_attr = chunk.source_url or chunk.source_id
        parts.append(
            f'<chunk id="{chunk.chunk_id}" source="{source_attr}" '
            f'document_id="{chunk.document_id}" source_id="{chunk.source_id}">'
        )
        parts.append(chunk.text)
        if chunk.parent_text:
            parts.append(f"[wider context: {chunk.parent_text}]")
        parts.append("</chunk>")
        parts.append("")

    parts.append("## Task")
    parts.append(
        "Call the `record_analysis` tool with your structured analysis. "
        "Every supporting_chunk_id must appear in the chunk id attributes listed above. "
        "Do not respond in prose."
    )

    return "\n".join(parts)
