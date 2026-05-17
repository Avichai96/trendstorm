"""Unit tests for AnalysisValidator.

All LLM calls are faked via a fake StructuredChatProvider.
No real API calls are made.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from trendstorm.domain.analyses.models import Analysis, Citation, Insight
from trendstorm.domain.categories.models import Category
from trendstorm.domain.llm.models import Message
from trendstorm.domain.retrieval.models import RetrievedChunk
from trendstorm.services.analysis.validator import (
    _VALIDATOR_TOOL_NAME,
    AnalysisValidator,
    ValidationResult,
    _format_user_message,
    _parse_validation_args,
)
from trendstorm.shared.config import AnalysisSettings
from trendstorm.shared.errors import LLMSchemaError
from trendstorm.shared.ids import new_id

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _settings(**overrides: object) -> AnalysisSettings:
    defaults: dict[str, object] = {
        "retrieval_k": 50, "rerank_k": 30, "final_k": 10,
        "query_expansion_count": 3, "validator_threshold": 0.75,
        "max_refinement_loops": 2,
    }
    defaults.update(overrides)
    return AnalysisSettings(**defaults)  # type: ignore[arg-type]


def _make_chunk(chunk_id: str | None = None, text: str = "evidence text") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id or new_id(),
        score=0.5,
        text=text,
        document_id=new_id(),
        source_id=new_id(),
    )


def _make_analysis(
    *,
    insights: list[Insight] | None = None,
    citations: list[Citation] | None = None,
    summary: str = "Test summary",
) -> Analysis:
    return Analysis(
        tenant_id=new_id(),
        job_id=new_id(),
        category_id=new_id(),
        summary=summary,
        insights=insights or [],
        citations=citations or [],
    )


def _fake_chat(args_to_return: dict, *, tool_name: str = _VALIDATOR_TOOL_NAME):
    """Fake StructuredChatProvider that returns the given tool args."""
    from trendstorm.domain.llm.models import TokenUsage
    chat = MagicMock()
    chat.model_id = "fake.model"
    chat.complete_with_tools = AsyncMock(
        return_value=(tool_name, args_to_return, TokenUsage(input_tokens=80, output_tokens=30))
    )
    return chat


# ---------------------------------------------------------------------------
# ValidationResult
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestValidationResult:
    def test_minimal_construction(self) -> None:
        r = ValidationResult(score=0.8, passed=True)
        assert r.score == 0.8
        assert r.passed is True
        assert r.notes == ""

    def test_score_bounds(self) -> None:
        with pytest.raises(ValidationError):
            ValidationResult(score=1.5, passed=False)
        with pytest.raises(ValidationError):
            ValidationResult(score=-0.1, passed=False)

    def test_meets_threshold_true_when_score_at_or_above(self) -> None:
        r = ValidationResult(score=0.75, passed=True)
        assert r.meets_threshold(0.75) is True
        assert r.meets_threshold(0.74) is True

    def test_meets_threshold_false_when_below(self) -> None:
        r = ValidationResult(score=0.6, passed=True)
        assert r.meets_threshold(0.75) is False

    def test_is_frozen(self) -> None:
        r = ValidationResult(score=0.5, passed=False)
        with pytest.raises(ValidationError):
            r.score = 0.9  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _parse_validation_args
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestParseValidationArgs:
    def test_happy_path(self) -> None:
        r = _parse_validation_args({"score": 0.85, "passed": True, "notes": "good"})
        assert r.score == 0.85
        assert r.passed is True
        assert r.notes == "good"

    def test_int_score_coerced_to_float(self) -> None:
        r = _parse_validation_args({"score": 1, "passed": True, "notes": ""})
        assert r.score == 1.0

    def test_score_clamped_to_upper_bound(self) -> None:
        r = _parse_validation_args({"score": 1.5, "passed": True, "notes": ""})
        assert r.score == 1.0

    def test_score_clamped_to_lower_bound(self) -> None:
        r = _parse_validation_args({"score": -0.3, "passed": False, "notes": ""})
        assert r.score == 0.0

    def test_missing_score_raises_schema_error(self) -> None:
        with pytest.raises(LLMSchemaError):
            _parse_validation_args({"passed": True, "notes": "no score"})

    def test_non_numeric_score_raises_schema_error(self) -> None:
        with pytest.raises(LLMSchemaError):
            _parse_validation_args({"score": "high", "passed": True, "notes": ""})

    def test_missing_passed_defaults_to_false(self) -> None:
        r = _parse_validation_args({"score": 0.5, "notes": "no passed field"})
        assert r.passed is False

    def test_missing_notes_defaults_to_empty(self) -> None:
        r = _parse_validation_args({"score": 0.5, "passed": True})
        assert r.notes == ""


# ---------------------------------------------------------------------------
# _format_user_message
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestFormatUserMessage:
    def test_includes_chunk_ids_in_corpus(self) -> None:
        chunks = [_make_chunk("c1", "first chunk text"), _make_chunk("c2", "second")]
        analysis = _make_analysis()
        msg = _format_user_message(analysis, chunks, category=None)
        assert "c1" in msg
        assert "c2" in msg
        assert "first chunk text" in msg

    def test_includes_summary(self) -> None:
        analysis = _make_analysis(summary="The key trend is X.")
        msg = _format_user_message(analysis, [], category=None)
        assert "The key trend is X." in msg

    def test_includes_insights_with_supporting_chunks(self) -> None:
        insight = Insight(
            claim="Trend X is rising.",
            rationale="Multiple sources confirm.",
            supporting_chunk_ids=["c1", "c2"],
            confidence=0.9,
            tags=["growth"],
        )
        analysis = _make_analysis(insights=[insight])
        msg = _format_user_message(analysis, [], category=None)
        assert "Trend X is rising." in msg
        assert "c1" in msg
        assert "c2" in msg
        assert "0.90" in msg or "0.9" in msg

    def test_includes_citations(self) -> None:
        cite = Citation(chunk_id="c1", document_id="d1", source_id="s1", excerpt="quoted text")
        analysis = _make_analysis(citations=[cite])
        msg = _format_user_message(analysis, [], category=None)
        assert "c1" in msg
        assert "quoted text" in msg

    def test_includes_category_brief_when_provided(self) -> None:
        category = Category(
            tenant_id=new_id(),
            name="AI Safety",
            description="Research on alignment.",
            keywords=["RLHF", "alignment"],
        )
        analysis = _make_analysis()
        msg = _format_user_message(analysis, [], category=category)
        assert "AI Safety" in msg
        assert "Research on alignment" in msg
        assert "RLHF" in msg

    def test_includes_parent_context_when_present(self) -> None:
        chunk = RetrievedChunk(
            chunk_id="c1",
            score=0.5,
            text="child text",
            parent_text="wider parent paragraph",
            document_id="d1",
            source_id="s1",
        )
        analysis = _make_analysis()
        msg = _format_user_message(analysis, [chunk], category=None)
        assert "child text" in msg
        assert "wider parent paragraph" in msg

    def test_handles_empty_inputs_gracefully(self) -> None:
        analysis = _make_analysis()
        msg = _format_user_message(analysis, [], category=None)
        assert "(no chunks provided)" in msg
        assert "(no citations)" in msg


# ---------------------------------------------------------------------------
# AnalysisValidator
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAnalysisValidator:
    async def test_returns_validation_result(self) -> None:
        chat = _fake_chat({"score": 0.85, "passed": True, "notes": "looks good"})
        v = AnalysisValidator(chat, _settings(), _prompt_text="STUB")
        result = await v.validate(_make_analysis(), [_make_chunk()])
        assert result.score == 0.85
        assert result.passed is True
        assert result.notes == "looks good"

    async def test_calls_chat_with_correct_tool_choice(self) -> None:
        chat = _fake_chat({"score": 0.5, "passed": False, "notes": "n"})
        v = AnalysisValidator(chat, _settings(), _prompt_text="STUB")
        await v.validate(_make_analysis(), [])
        call_kwargs = chat.complete_with_tools.call_args.kwargs
        assert call_kwargs["tool_choice"] == _VALIDATOR_TOOL_NAME

    async def test_calls_chat_with_record_validation_tool_definition(self) -> None:
        chat = _fake_chat({"score": 0.5, "passed": False, "notes": ""})
        v = AnalysisValidator(chat, _settings(), _prompt_text="STUB")
        await v.validate(_make_analysis(), [])
        tools = chat.complete_with_tools.call_args.kwargs["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == _VALIDATOR_TOOL_NAME
        # Schema must include the three required fields
        required = tools[0]["input_schema"]["required"]
        assert set(required) == {"score", "passed", "notes"}

    async def test_system_prompt_passed_as_first_message(self) -> None:
        chat = _fake_chat({"score": 0.5, "passed": False, "notes": ""})
        v = AnalysisValidator(chat, _settings(), _prompt_text="CUSTOM-VALIDATOR-PROMPT")
        await v.validate(_make_analysis(), [])
        messages: list[Message] = chat.complete_with_tools.call_args.args[0]
        assert messages[0].role == "system"
        assert "CUSTOM-VALIDATOR-PROMPT" in messages[0].content

    async def test_user_message_contains_analysis_summary(self) -> None:
        chat = _fake_chat({"score": 0.5, "passed": False, "notes": ""})
        v = AnalysisValidator(chat, _settings(), _prompt_text="STUB")
        analysis = _make_analysis(summary="Unique-summary-marker-XYZ")
        await v.validate(analysis, [])
        messages: list[Message] = chat.complete_with_tools.call_args.args[0]
        assert any("Unique-summary-marker-XYZ" in m.content for m in messages)

    async def test_raises_when_chat_returns_wrong_tool(self) -> None:
        chat = _fake_chat(
            {"score": 0.5, "passed": False, "notes": ""},
            tool_name="some_other_tool",
        )
        v = AnalysisValidator(chat, _settings(), _prompt_text="STUB")
        with pytest.raises(LLMSchemaError, match="unexpected tool"):
            await v.validate(_make_analysis(), [])

    async def test_meets_threshold_uses_settings(self) -> None:
        chat = _fake_chat({"score": 0.80, "passed": True, "notes": ""})
        s = _settings(validator_threshold=0.75)
        v = AnalysisValidator(chat, s, _prompt_text="STUB")
        result = await v.validate(_make_analysis(), [])
        assert result.meets_threshold(s.validator_threshold) is True

    async def test_schema_error_from_chat_propagates(self) -> None:
        chat = MagicMock()
        chat.model_id = "fake.model"
        chat.complete_with_tools = AsyncMock(side_effect=LLMSchemaError("no tool block"))
        v = AnalysisValidator(chat, _settings(), _prompt_text="STUB")
        with pytest.raises(LLMSchemaError):
            await v.validate(_make_analysis(), [])
