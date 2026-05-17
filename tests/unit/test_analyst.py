"""Unit tests for the Analyst service.

All dependencies are faked: HybridRetriever, StructuredChatProvider, and
AnalysisValidator. No real retrieval, no real LLM, no Mongo.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from trendstorm.domain.categories.models import Category
from trendstorm.domain.llm.models import TokenUsage
from trendstorm.domain.retrieval.models import RetrievedChunk
from trendstorm.services.analysis.analyst import (
    _ANALYST_TOOL_NAME,
    _ANALYST_TOOL_SCHEMA,
    AnalysisResult,
    Analyst,
    _build_analysis_from_tool_args,
    _format_user_message,
)
from trendstorm.services.analysis.validator import ValidationResult
from trendstorm.shared.config import AnalysisSettings
from trendstorm.shared.errors import LLMSchemaError, ValidationError
from trendstorm.shared.ids import new_id

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _settings(**overrides: object) -> AnalysisSettings:
    defaults: dict[str, object] = {
        "retrieval_k": 50, "rerank_k": 30, "final_k": 5,
        "query_expansion_count": 3, "validator_threshold": 0.75,
        "max_refinement_loops": 2,
    }
    defaults.update(overrides)
    return AnalysisSettings(**defaults)  # type: ignore[arg-type]


def _make_category(name: str = "AI Safety", description: str = "alignment research") -> Category:
    return Category(
        tenant_id=new_id(),
        name=name,
        description=description,
        keywords=["RLHF", "interpretability"],
    )


def _make_chunk(chunk_id: str | None = None, text: str = "evidence") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id or new_id(),
        score=0.5,
        text=text,
        document_id=new_id(),
        source_id=new_id(),
    )


def _fake_retriever(chunks: list[RetrievedChunk]) -> Any:
    r = MagicMock()
    r.retrieve = AsyncMock(return_value=chunks)
    return r


def _fake_chat(args: dict[str, Any], *, tool_name: str = _ANALYST_TOOL_NAME) -> Any:
    chat = MagicMock()
    chat.model_id = "anthropic.claude-sonnet-4-6"
    chat.complete_with_tools = AsyncMock(
        return_value=(tool_name, args, TokenUsage(input_tokens=100, output_tokens=50))
    )
    return chat


def _fake_validator(score: float = 0.85, passed: bool = True, notes: str = "good") -> Any:
    v = MagicMock()
    v.validate = AsyncMock(
        return_value=ValidationResult(score=score, passed=passed, notes=notes)
    )
    return v


def _good_analyst_args(*, chunk_ids: list[str], summary: str = "Synthesised summary.") -> dict:
    return {
        "summary": summary,
        "insights": [
            {
                "claim": "Trend A is rising in evidence.",
                "rationale": "Multiple chunks confirm.",
                "supporting_chunk_ids": chunk_ids[:2],
                "confidence": 0.9,
                "tags": ["growth"],
            },
        ],
        "citations": [
            {
                "chunk_id": chunk_ids[0],
                "document_id": "d1",
                "source_id": "s1",
                "excerpt": "Some quoted evidence.",
            },
            {
                "chunk_id": chunk_ids[1],
                "document_id": "d2",
                "source_id": "s2",
                "excerpt": "More quoted evidence.",
            },
        ],
    }


# ---------------------------------------------------------------------------
# _build_analysis_from_tool_args
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestBuildAnalysisFromToolArgs:
    def _kwargs(self, **overrides: object) -> dict[str, object]:
        defaults: dict[str, object] = {
            "tenant_id": "t1",
            "job_id": "j1",
            "category_id": "cat1",
            "model_id": "anthropic.claude-sonnet-4-6",
            "refinement_loop": 0,
            "valid_chunk_ids": {"c1", "c2", "c3"},
        }
        defaults.update(overrides)
        return defaults

    def test_happy_path(self) -> None:
        args = {
            "summary": "Summary text.",
            "insights": [
                {
                    "claim": "X is rising.",
                    "supporting_chunk_ids": ["c1", "c2"],
                    "confidence": 0.8,
                }
            ],
            "citations": [
                {"chunk_id": "c1", "document_id": "d1", "source_id": "s1", "excerpt": "ex1"},
            ],
        }
        a = _build_analysis_from_tool_args(args=args, **self._kwargs())  # type: ignore[arg-type]
        assert a.summary == "Summary text."
        assert len(a.insights) == 1
        assert len(a.citations) == 1
        assert a.model_provider == "anthropic"
        assert a.model_name == "claude-sonnet-4-6"
        assert a.tenant_id == "t1"
        assert a.refinement_loops == 0

    def test_hallucinated_supporting_chunk_ids_are_dropped(self) -> None:
        args = {
            "summary": "Summary.",
            "insights": [
                {
                    "claim": "Real claim",
                    "supporting_chunk_ids": ["c1", "FAKE_ID"],
                    "confidence": 0.5,
                }
            ],
            "citations": [
                {"chunk_id": "c1", "document_id": "d", "source_id": "s", "excerpt": "ex"},
            ],
        }
        a = _build_analysis_from_tool_args(args=args, **self._kwargs())  # type: ignore[arg-type]
        assert a.insights[0].supporting_chunk_ids == ["c1"]  # FAKE_ID dropped

    def test_insight_with_no_valid_supporting_chunks_is_dropped(self) -> None:
        args = {
            "summary": "Summary.",
            "insights": [
                {
                    "claim": "Bad claim",
                    "supporting_chunk_ids": ["FAKE1", "FAKE2"],
                    "confidence": 0.9,
                },
                {
                    "claim": "Good claim",
                    "supporting_chunk_ids": ["c1"],
                    "confidence": 0.9,
                },
            ],
            "citations": [
                {"chunk_id": "c1", "document_id": "d", "source_id": "s", "excerpt": "ex"},
            ],
        }
        a = _build_analysis_from_tool_args(args=args, **self._kwargs())  # type: ignore[arg-type]
        assert len(a.insights) == 1
        assert a.insights[0].claim == "Good claim"

    def test_citations_not_in_corpus_are_dropped(self) -> None:
        args = {
            "summary": "Summary.",
            "insights": [
                {"claim": "Claim", "supporting_chunk_ids": ["c1"], "confidence": 0.5}
            ],
            "citations": [
                {"chunk_id": "c1", "document_id": "d", "source_id": "s", "excerpt": "ex"},
                {"chunk_id": "FAKE", "document_id": "d", "source_id": "s", "excerpt": "ex"},
            ],
        }
        a = _build_analysis_from_tool_args(args=args, **self._kwargs())  # type: ignore[arg-type]
        assert len(a.citations) == 1
        assert a.citations[0].chunk_id == "c1"

    def test_duplicate_citations_deduplicated(self) -> None:
        args = {
            "summary": "Summary.",
            "insights": [
                {"claim": "C", "supporting_chunk_ids": ["c1"], "confidence": 0.5}
            ],
            "citations": [
                {"chunk_id": "c1", "document_id": "d", "source_id": "s", "excerpt": "first"},
                {"chunk_id": "c1", "document_id": "d", "source_id": "s", "excerpt": "dup"},
            ],
        }
        a = _build_analysis_from_tool_args(args=args, **self._kwargs())  # type: ignore[arg-type]
        assert len(a.citations) == 1

    def test_overlong_excerpts_truncated_to_500(self) -> None:
        long_excerpt = "x" * 1000
        args = {
            "summary": "Summary.",
            "insights": [
                {"claim": "C", "supporting_chunk_ids": ["c1"], "confidence": 0.5}
            ],
            "citations": [
                {"chunk_id": "c1", "document_id": "d", "source_id": "s", "excerpt": long_excerpt},
            ],
        }
        a = _build_analysis_from_tool_args(args=args, **self._kwargs())  # type: ignore[arg-type]
        assert len(a.citations[0].excerpt) == 500

    def test_empty_summary_raises_schema_error(self) -> None:
        args = {"summary": "", "insights": [], "citations": []}
        with pytest.raises(LLMSchemaError, match="empty summary"):
            _build_analysis_from_tool_args(args=args, **self._kwargs())  # type: ignore[arg-type]

    def test_missing_summary_raises_schema_error(self) -> None:
        args = {"insights": [], "citations": []}
        with pytest.raises(LLMSchemaError):
            _build_analysis_from_tool_args(args=args, **self._kwargs())  # type: ignore[arg-type]

    def test_unknown_model_id_format_handled(self) -> None:
        args = {
            "summary": "S",
            "insights": [{"claim": "C", "supporting_chunk_ids": ["c1"], "confidence": 0.5}],
            "citations": [{"chunk_id": "c1", "document_id": "d", "source_id": "s", "excerpt": "ex"}],
        }
        a = _build_analysis_from_tool_args(
            args=args, **self._kwargs(model_id="single-token-no-dot")  # type: ignore[arg-type]
        )
        assert a.model_provider == "unknown"
        assert a.model_name == "single-token-no-dot"


# ---------------------------------------------------------------------------
# _format_user_message
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestFormatUserMessage:
    def test_includes_category_brief(self) -> None:
        cat = _make_category()
        msg = _format_user_message(category=cat, chunks=[], refinement_notes=None)
        assert cat.name in msg
        assert "alignment research" in msg
        assert "RLHF" in msg

    def test_includes_chunks_with_ids(self) -> None:
        chunks = [_make_chunk("c_alpha", "first"), _make_chunk("c_beta", "second")]
        msg = _format_user_message(category=_make_category(), chunks=chunks, refinement_notes=None)
        assert "c_alpha" in msg
        assert "c_beta" in msg
        assert "first" in msg
        assert "second" in msg

    def test_refinement_notes_appear_before_evidence(self) -> None:
        msg = _format_user_message(
            category=_make_category(),
            chunks=[_make_chunk(text="zzz_evidence_marker")],
            refinement_notes="zzz_validator_feedback",
        )
        feedback_pos = msg.find("zzz_validator_feedback")
        evidence_pos = msg.find("zzz_evidence_marker")
        assert feedback_pos != -1 and evidence_pos != -1
        assert feedback_pos < evidence_pos

    def test_includes_parent_text_when_present(self) -> None:
        chunk = RetrievedChunk(
            chunk_id="c1",
            score=0.5,
            text="child",
            parent_text="WIDER_PARENT_PARAGRAPH",
            document_id="d1",
            source_id="s1",
        )
        msg = _format_user_message(category=_make_category(), chunks=[chunk], refinement_notes=None)
        assert "WIDER_PARENT_PARAGRAPH" in msg

    def test_mentions_tool_name_in_task(self) -> None:
        msg = _format_user_message(category=_make_category(), chunks=[], refinement_notes=None)
        assert "record_analysis" in msg

    def test_omits_refinement_section_when_notes_none(self) -> None:
        msg = _format_user_message(category=_make_category(), chunks=[], refinement_notes=None)
        assert "Validator Feedback" not in msg


# ---------------------------------------------------------------------------
# Analyst.produce_analysis
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAnalystProduceAnalysis:
    async def test_happy_path_returns_analysis_result(self) -> None:
        cid1, cid2, cid3 = new_id(), new_id(), new_id()
        chunks = [_make_chunk(cid1), _make_chunk(cid2), _make_chunk(cid3)]
        retriever = _fake_retriever(chunks)
        chat = _fake_chat(_good_analyst_args(chunk_ids=[cid1, cid2, cid3]))
        validator = _fake_validator(score=0.85)

        a = Analyst(retriever, chat, validator, _settings(), _prompt_text="STUB")
        category = _make_category()

        result = await a.produce_analysis(
            category,
            tenant_id="t1",
            job_id="j1",
        )

        assert isinstance(result, AnalysisResult)
        assert result.analysis.tenant_id == "t1"
        assert result.analysis.job_id == "j1"
        assert result.analysis.category_id == category.id
        assert result.analysis.summary == "Synthesised summary."
        assert len(result.analysis.insights) == 1
        assert result.validation.score == 0.85
        assert result.validation.passed is True

    async def test_validator_scores_stamped_onto_analysis(self) -> None:
        cid = new_id()
        retriever = _fake_retriever([_make_chunk(cid)])
        chat = _fake_chat(_good_analyst_args(chunk_ids=[cid, new_id()]))
        validator = _fake_validator(score=0.62, passed=False, notes="grounding weak")

        a = Analyst(retriever, chat, validator, _settings(), _prompt_text="STUB")
        result = await a.produce_analysis(
            _make_category(), tenant_id="t", job_id="j",
        )

        assert result.analysis.validator_score == 0.62
        assert result.analysis.validator_passed is False
        assert result.analysis.validator_notes == "grounding weak"

    async def test_meets_threshold_helper(self) -> None:
        cid = new_id()
        retriever = _fake_retriever([_make_chunk(cid)])
        chat = _fake_chat(_good_analyst_args(chunk_ids=[cid, new_id()]))
        validator = _fake_validator(score=0.80)

        a = Analyst(retriever, chat, validator, _settings(validator_threshold=0.75), _prompt_text="STUB")
        result = await a.produce_analysis(_make_category(), tenant_id="t", job_id="j")
        assert result.meets_threshold(0.75) is True
        assert result.meets_threshold(0.85) is False

    async def test_empty_chunks_raises_validation_error(self) -> None:
        retriever = _fake_retriever([])
        a = Analyst(retriever, _fake_chat({}), _fake_validator(), _settings(), _prompt_text="STUB")
        with pytest.raises(ValidationError, match="zero retrieved chunks"):
            await a.produce_analysis(_make_category(), tenant_id="t", job_id="j")

    async def test_refinement_notes_added_to_retrieval_query(self) -> None:
        cid = new_id()
        retriever = _fake_retriever([_make_chunk(cid)])
        chat = _fake_chat(_good_analyst_args(chunk_ids=[cid, new_id()]))
        validator = _fake_validator()
        a = Analyst(retriever, chat, validator, _settings(), _prompt_text="STUB")

        await a.produce_analysis(
            _make_category(),
            tenant_id="t", job_id="j",
            query="base query",
            refinement_notes="missing coverage of topic X",
        )

        request = retriever.retrieve.call_args.args[0]
        assert "base query" in request.query
        assert "missing coverage of topic X" in request.query

    async def test_refinement_loop_recorded_on_analysis(self) -> None:
        cid = new_id()
        retriever = _fake_retriever([_make_chunk(cid)])
        chat = _fake_chat(_good_analyst_args(chunk_ids=[cid, new_id()]))
        a = Analyst(retriever, chat, _fake_validator(), _settings(), _prompt_text="STUB")

        result = await a.produce_analysis(
            _make_category(), tenant_id="t", job_id="j", refinement_loop=2,
        )
        assert result.analysis.refinement_loops == 2

    async def test_default_query_uses_category_name_and_description(self) -> None:
        cid = new_id()
        retriever = _fake_retriever([_make_chunk(cid)])
        chat = _fake_chat(_good_analyst_args(chunk_ids=[cid, new_id()]))
        a = Analyst(retriever, chat, _fake_validator(), _settings(), _prompt_text="STUB")

        cat = _make_category(name="AI Safety", description="alignment research")
        await a.produce_analysis(cat, tenant_id="t", job_id="j")

        request = retriever.retrieve.call_args.args[0]
        assert "AI Safety" in request.query
        assert "alignment research" in request.query

    async def test_custom_query_overrides_default(self) -> None:
        cid = new_id()
        retriever = _fake_retriever([_make_chunk(cid)])
        chat = _fake_chat(_good_analyst_args(chunk_ids=[cid, new_id()]))
        a = Analyst(retriever, chat, _fake_validator(), _settings(), _prompt_text="STUB")

        await a.produce_analysis(
            _make_category(), tenant_id="t", job_id="j",
            query="my-custom-search-query",
        )
        request = retriever.retrieve.call_args.args[0]
        assert "my-custom-search-query" in request.query

    async def test_wrong_tool_name_raises_schema_error(self) -> None:
        cid = new_id()
        retriever = _fake_retriever([_make_chunk(cid)])
        chat = _fake_chat(
            _good_analyst_args(chunk_ids=[cid, new_id()]),
            tool_name="record_NOT_analysis",
        )
        a = Analyst(retriever, chat, _fake_validator(), _settings(), _prompt_text="STUB")

        with pytest.raises(LLMSchemaError, match="unexpected tool"):
            await a.produce_analysis(_make_category(), tenant_id="t", job_id="j")

    async def test_chat_called_with_record_analysis_tool_choice(self) -> None:
        cid = new_id()
        retriever = _fake_retriever([_make_chunk(cid)])
        chat = _fake_chat(_good_analyst_args(chunk_ids=[cid, new_id()]))
        a = Analyst(retriever, chat, _fake_validator(), _settings(), _prompt_text="STUB")

        await a.produce_analysis(_make_category(), tenant_id="t", job_id="j")
        kwargs = chat.complete_with_tools.call_args.kwargs
        assert kwargs["tool_choice"] == _ANALYST_TOOL_NAME
        assert kwargs["tools"][0]["name"] == _ANALYST_TOOL_NAME

    async def test_top_k_uses_settings_final_k(self) -> None:
        cid = new_id()
        retriever = _fake_retriever([_make_chunk(cid)])
        chat = _fake_chat(_good_analyst_args(chunk_ids=[cid, new_id()]))
        a = Analyst(retriever, chat, _fake_validator(), _settings(final_k=7), _prompt_text="STUB")

        await a.produce_analysis(_make_category(), tenant_id="t", job_id="j")
        request = retriever.retrieve.call_args.args[0]
        assert request.top_k == 7

    async def test_validator_receives_analysis_and_chunks(self) -> None:
        cid1, cid2 = new_id(), new_id()
        chunks = [_make_chunk(cid1), _make_chunk(cid2)]
        retriever = _fake_retriever(chunks)
        chat = _fake_chat(_good_analyst_args(chunk_ids=[cid1, cid2]))
        validator = _fake_validator()
        a = Analyst(retriever, chat, validator, _settings(), _prompt_text="STUB")

        cat = _make_category()
        await a.produce_analysis(cat, tenant_id="t", job_id="j")

        validator.validate.assert_called_once()
        _, chunks_arg = validator.validate.call_args.args[:2]
        assert chunks_arg is chunks


# ---------------------------------------------------------------------------
# Tool schema sanity
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAnalystToolSchema:
    def test_tool_name_constant(self) -> None:
        assert _ANALYST_TOOL_SCHEMA["name"] == "record_analysis"
        assert _ANALYST_TOOL_NAME == "record_analysis"

    def test_required_fields(self) -> None:
        required = _ANALYST_TOOL_SCHEMA["input_schema"]["required"]
        assert set(required) == {"summary", "insights", "citations"}

    def test_insight_has_required_chunk_grounding(self) -> None:
        insight_required = (
            _ANALYST_TOOL_SCHEMA["input_schema"]["properties"]["insights"]["items"]["required"]
        )
        assert "supporting_chunk_ids" in insight_required

    def test_citation_has_required_provenance(self) -> None:
        citation_required = (
            _ANALYST_TOOL_SCHEMA["input_schema"]["properties"]["citations"]["items"]["required"]
        )
        assert {"chunk_id", "document_id", "source_id", "excerpt"} <= set(citation_required)
