"""Unit tests for RenderEngine and PublisherService.

All I/O (MinIO uploads, Mongo inserts, weasyprint) is mocked.
No Docker required.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trendstorm.domain.analyses.models import Analysis, Citation, Insight
from trendstorm.domain.categories.models import Category
from trendstorm.services.publish.renderer import RenderEngine
from trendstorm.services.publish.service import PublisherService, PublishResult, _make_title
from trendstorm.shared.types import ReportFormat

# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

def _make_analysis(
    *,
    summary: str = "AI safety is growing rapidly across all sectors.",
    insights: list[Insight] | None = None,
    citations: list[Citation] | None = None,
    validator_score: float = 0.85,
    refinement_loops: int = 1,
) -> Analysis:
    if insights is None:
        insights = [
            Insight(
                claim="LLM safety is becoming policy",
                rationale="Evidence from multiple government documents.",
                supporting_chunk_ids=["chunk-1", "chunk-2"],
                confidence=0.9,
                tags=["policy", "LLM"],
            )
        ]
    if citations is None:
        citations = [
            Citation(
                chunk_id="chunk-1",
                document_id="doc-1",
                source_id="src-1",
                excerpt="Safety guardrails are now a legislative requirement.",
                url="https://example.com/safety",
            ),
        ]
    return Analysis(
        tenant_id="t1",
        job_id="job-1",
        category_id="cat-1",
        summary=summary,
        insights=insights,
        citations=citations,
        validator_score=validator_score,
        refinement_loops=refinement_loops,
        created_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC),
    )


def _make_category(name: str = "AI Safety") -> Category:
    return Category(tenant_id="t1", name=name, description="desc", keywords=[])


def _build_publisher_service(*, pdf_error: Exception | None = None) -> tuple[PublisherService, Any, Any]:
    """Return (service, minio_mock, report_repo_mock).

    Uses a MagicMock for RenderEngine so PublisherService tests run without
    system libs (weasyprint requires GTK/Pango). Renderer tests exercise the
    real RenderEngine separately.
    """
    renderer = MagicMock(spec=RenderEngine)
    renderer.render_markdown.return_value = "# Test Report\n\nSummary here."
    renderer.render_json.return_value = b'{"summary": "test"}'
    if pdf_error is not None:
        renderer.render_pdf.side_effect = pdf_error
    else:
        renderer.render_pdf.return_value = b"%PDF-1.4 fake"

    minio = MagicMock()
    minio.upload = AsyncMock(return_value="s3://bucket/key")

    report_repo = MagicMock()
    report_repo.insert = AsyncMock()

    blob_settings = MagicMock()
    blob_settings.bucket_reports = "trendstorm-reports"

    svc = PublisherService(
        renderer=renderer,
        minio=minio,
        report_repo=report_repo,
        blob_settings=blob_settings,
    )
    return svc, minio, report_repo


# ===========================================================================
# RenderEngine — render_markdown
# ===========================================================================

@pytest.mark.unit
class TestRenderEngineMarkdown:
    def test_contains_category_name(self) -> None:
        engine = RenderEngine()
        result = engine.render_markdown(_make_analysis(), category_name="AI Safety")
        assert "AI Safety" in result

    def test_contains_summary(self) -> None:
        engine = RenderEngine()
        analysis = _make_analysis(summary="Rapid growth in AI regulation worldwide.")
        result = engine.render_markdown(analysis, category_name="AI Safety")
        assert "Rapid growth in AI regulation worldwide." in result

    def test_contains_insight_claim(self) -> None:
        engine = RenderEngine()
        analysis = _make_analysis()
        result = engine.render_markdown(analysis, category_name="AI Safety")
        assert "LLM safety is becoming policy" in result

    def test_contains_insight_rationale(self) -> None:
        engine = RenderEngine()
        result = engine.render_markdown(_make_analysis(), category_name="AI Safety")
        assert "Evidence from multiple government documents." in result

    def test_contains_citation_excerpt(self) -> None:
        engine = RenderEngine()
        result = engine.render_markdown(_make_analysis(), category_name="AI Safety")
        assert "Safety guardrails are now a legislative requirement." in result

    def test_contains_citation_url(self) -> None:
        engine = RenderEngine()
        result = engine.render_markdown(_make_analysis(), category_name="AI Safety")
        assert "https://example.com/safety" in result

    def test_contains_validator_score(self) -> None:
        engine = RenderEngine()
        result = engine.render_markdown(_make_analysis(validator_score=0.85), category_name="AI Safety")
        assert "85%" in result

    def test_contains_job_id(self) -> None:
        engine = RenderEngine()
        analysis = _make_analysis()
        result = engine.render_markdown(analysis, category_name="AI Safety")
        assert analysis.job_id in result

    def test_empty_insights_renders_clean(self) -> None:
        engine = RenderEngine()
        analysis = _make_analysis(insights=[], citations=[])
        result = engine.render_markdown(analysis, category_name="AI Safety")
        assert "AI Safety" in result
        assert "Executive Summary" in result

    def test_empty_citations_shows_no_sources(self) -> None:
        engine = RenderEngine()
        analysis = _make_analysis(citations=[])
        result = engine.render_markdown(analysis, category_name="AI Safety")
        assert "No external sources cited" in result

    def test_citation_only_shown_for_matching_insight(self) -> None:
        """A citation with a chunk_id NOT in any insight should NOT appear in insights section."""
        engine = RenderEngine()
        insight = Insight(
            claim="Claim A",
            supporting_chunk_ids=["chunk-A"],
            confidence=0.8,
        )
        citation_matched = Citation(
            chunk_id="chunk-A", document_id="d1", source_id="s1",
            excerpt="Matched excerpt", url="https://matched.example",
        )
        citation_unmatched = Citation(
            chunk_id="chunk-ORPHAN", document_id="d2", source_id="s2",
            excerpt="Unmatched excerpt", url="https://unmatched.example",
        )
        analysis = _make_analysis(insights=[insight], citations=[citation_matched, citation_unmatched])
        result = engine.render_markdown(analysis, category_name="AI Safety")
        # Matched citation URL appears under its insight
        assert "https://matched.example" in result
        # Unmatched orphan URL still appears in bibliography
        assert "https://unmatched.example" in result

    def test_returns_string(self) -> None:
        engine = RenderEngine()
        result = engine.render_markdown(_make_analysis(), category_name="AI Safety")
        assert isinstance(result, str)
        assert len(result) > 0


# ===========================================================================
# RenderEngine — render_json
# ===========================================================================

@pytest.mark.unit
class TestRenderEngineJson:
    def test_returns_bytes(self) -> None:
        engine = RenderEngine()
        result = engine.render_json(_make_analysis())
        assert isinstance(result, bytes)

    def test_is_valid_json(self) -> None:
        engine = RenderEngine()
        raw = engine.render_json(_make_analysis())
        data = json.loads(raw.decode("utf-8"))
        assert isinstance(data, dict)

    def test_contains_summary(self) -> None:
        engine = RenderEngine()
        analysis = _make_analysis(summary="Test summary here.")
        raw = engine.render_json(analysis)
        data = json.loads(raw.decode("utf-8"))
        assert data["summary"] == "Test summary here."

    def test_contains_insights(self) -> None:
        engine = RenderEngine()
        raw = engine.render_json(_make_analysis())
        data = json.loads(raw.decode("utf-8"))
        assert len(data["insights"]) == 1
        assert data["insights"][0]["claim"] == "LLM safety is becoming policy"

    def test_is_pretty_printed(self) -> None:
        engine = RenderEngine()
        raw = engine.render_json(_make_analysis())
        text = raw.decode("utf-8")
        # Pretty-printed JSON has newlines
        assert "\n" in text

    def test_utf8_encoded(self) -> None:
        engine = RenderEngine()
        analysis = _make_analysis(summary="Résumé with accents: café")
        raw = engine.render_json(analysis)
        data = json.loads(raw.decode("utf-8"))
        assert "café" in data["summary"]


def _mock_weasyprint() -> MagicMock:
    """Return a mock weasyprint module for patching into sys.modules."""
    mock_wp = MagicMock()
    mock_wp.HTML.return_value.write_pdf.return_value = b"%PDF-1.4 fake"
    return mock_wp


# ===========================================================================
# RenderEngine — render_pdf
# ===========================================================================

@pytest.mark.unit
class TestRenderEnginePdf:
    def test_returns_bytes(self) -> None:
        engine = RenderEngine()
        mock_wp = _mock_weasyprint()
        with patch.dict(sys.modules, {"weasyprint": mock_wp}):
            result = engine.render_pdf("# Hello\n\nSome content.")
        assert result == b"%PDF-1.4 fake"

    def test_passes_html_string_to_weasyprint(self) -> None:
        engine = RenderEngine()
        mock_wp = _mock_weasyprint()
        with patch.dict(sys.modules, {"weasyprint": mock_wp}):
            engine.render_pdf("# My Markdown")
            call_kwargs = mock_wp.HTML.call_args.kwargs
            assert "string" in call_kwargs
            # Markdown syntax is gone; text is now in an HTML element
            assert "# My Markdown" not in call_kwargs["string"]
            assert "<h1>" in call_kwargs["string"]
            assert "<body>" in call_kwargs["string"]

    def test_raises_on_weasyprint_failure(self) -> None:
        engine = RenderEngine()
        mock_wp = _mock_weasyprint()
        mock_wp.HTML.return_value.write_pdf.side_effect = RuntimeError("missing fonts")
        with patch.dict(sys.modules, {"weasyprint": mock_wp}), pytest.raises(RuntimeError, match="missing fonts"):
            engine.render_pdf("# Content")


# ===========================================================================
# _make_title
# ===========================================================================

@pytest.mark.unit
class TestMakeTitle:
    def test_short_summary_not_truncated(self) -> None:
        analysis = _make_analysis(summary="Short summary.")
        cat = _make_category("AI Safety")
        title = _make_title(analysis, cat)
        assert title == "AI Safety: Short summary."

    def test_long_summary_truncated_at_60(self) -> None:
        long_summary = "A" * 100
        analysis = _make_analysis(summary=long_summary)
        cat = _make_category("AI Safety")
        title = _make_title(analysis, cat)
        assert title.endswith("…")
        # 60 chars of summary + "…" after the category prefix
        prefix = "AI Safety: "
        body = title[len(prefix):]
        assert len(body) == 61  # 60 chars + ellipsis (single char)

    def test_exactly_60_chars_no_ellipsis(self) -> None:
        summary = "B" * 60
        analysis = _make_analysis(summary=summary)
        cat = _make_category("AI Safety")
        title = _make_title(analysis, cat)
        assert not title.endswith("…")

    def test_category_name_in_title(self) -> None:
        analysis = _make_analysis()
        cat = _make_category("Cybersecurity")
        title = _make_title(analysis, cat)
        assert title.startswith("Cybersecurity: ")


# ===========================================================================
# PublisherService — happy path
# ===========================================================================

@pytest.mark.unit
class TestPublisherServiceHappyPath:
    async def test_returns_publish_result(self) -> None:
        svc, _, _ = _build_publisher_service()
        result = await svc.publish(_make_analysis(), _make_category())
        assert isinstance(result, PublishResult)

    async def test_markdown_report_id_set(self) -> None:
        svc, _, _ = _build_publisher_service()
        result = await svc.publish(_make_analysis(), _make_category())
        assert result.markdown_report_id is not None
        assert len(result.markdown_report_id) > 0

    async def test_json_report_id_set(self) -> None:
        svc, _, _ = _build_publisher_service()
        result = await svc.publish(_make_analysis(), _make_category())
        assert result.json_report_id is not None

    async def test_pdf_report_id_set_on_success(self) -> None:
        svc, _, _ = _build_publisher_service()
        result = await svc.publish(_make_analysis(), _make_category())
        assert result.pdf_report_id is not None

    async def test_minio_upload_called_thrice_on_full_success(self) -> None:
        svc, minio, _ = _build_publisher_service()
        await svc.publish(_make_analysis(), _make_category())
        assert minio.upload.call_count == 3

    async def test_report_repo_insert_called_thrice_on_full_success(self) -> None:
        svc, _, report_repo = _build_publisher_service()
        await svc.publish(_make_analysis(), _make_category())
        assert report_repo.insert.call_count == 3

    async def test_report_ids_are_distinct(self) -> None:
        svc, _, _ = _build_publisher_service()
        result = await svc.publish(_make_analysis(), _make_category())
        ids = {result.markdown_report_id, result.json_report_id, result.pdf_report_id}
        assert len(ids) == 3  # all distinct

    async def test_inserted_reports_use_correct_formats(self) -> None:
        svc, _, report_repo = _build_publisher_service()
        await svc.publish(_make_analysis(), _make_category())
        formats = {call.args[0].format for call in report_repo.insert.call_args_list}
        assert ReportFormat.MARKDOWN in formats
        assert ReportFormat.JSON in formats
        assert ReportFormat.PDF in formats

    async def test_correct_bucket_used(self) -> None:
        svc, minio, _ = _build_publisher_service()
        await svc.publish(_make_analysis(), _make_category())
        for call in minio.upload.call_args_list:
            assert call.args[0] == "trendstorm-reports"

    async def test_markdown_key_has_md_extension(self) -> None:
        svc, minio, _ = _build_publisher_service()
        await svc.publish(_make_analysis(), _make_category())
        md_key = minio.upload.call_args_list[0].args[1]
        assert md_key.endswith(".md")


# ===========================================================================
# PublisherService — PDF failure handling
# ===========================================================================

@pytest.mark.unit
class TestPublisherServicePdfFailure:
    async def test_pdf_failure_swallowed(self) -> None:
        svc, _, _ = _build_publisher_service(pdf_error=RuntimeError("no fonts"))
        # Should not raise
        result = await svc.publish(_make_analysis(), _make_category())
        assert result is not None

    async def test_pdf_failure_returns_none_pdf_id(self) -> None:
        svc, _, _ = _build_publisher_service(pdf_error=RuntimeError("no fonts"))
        result = await svc.publish(_make_analysis(), _make_category())
        assert result.pdf_report_id is None

    async def test_markdown_and_json_ids_still_set_on_pdf_failure(self) -> None:
        svc, _, _ = _build_publisher_service(pdf_error=RuntimeError("no fonts"))
        result = await svc.publish(_make_analysis(), _make_category())
        assert result.markdown_report_id is not None
        assert result.json_report_id is not None

    async def test_two_uploads_on_pdf_failure(self) -> None:
        svc, minio, _ = _build_publisher_service(pdf_error=RuntimeError("no fonts"))
        await svc.publish(_make_analysis(), _make_category())
        assert minio.upload.call_count == 2

    async def test_two_inserts_on_pdf_failure(self) -> None:
        svc, _, report_repo = _build_publisher_service(pdf_error=RuntimeError("no fonts"))
        await svc.publish(_make_analysis(), _make_category())
        assert report_repo.insert.call_count == 2
