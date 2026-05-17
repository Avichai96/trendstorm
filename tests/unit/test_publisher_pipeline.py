"""Unit tests for PublisherPipeline.

Tests confirm that the pipeline wires repository lookups and service calls
correctly. PublisherService is mocked — its own tests are in test_publisher.py.
No Docker required.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trendstorm.agents.publisher.pipeline import PublisherPipeline, PublishPipelineResult
from trendstorm.domain.analyses.models import Analysis
from trendstorm.domain.categories.models import Category
from trendstorm.services.publish.service import PublishResult
from trendstorm.shared.errors import NotFoundError
from trendstorm.shared.ids import new_id

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_analysis(tenant_id: str = "t1", job_id: str = "job-1") -> Analysis:
    return Analysis(
        tenant_id=tenant_id,
        job_id=job_id,
        category_id="cat-1",
        summary="AI safety landscape.",
    )


def _make_category(tenant_id: str = "t1") -> Category:
    return Category(tenant_id=tenant_id, name="AI Safety", description="desc", keywords=[])


def _build_pipeline(
    *,
    analysis: Analysis | None = None,
    category: Category | None = None,
) -> tuple[PublisherPipeline, MagicMock]:
    """Return (pipeline, mock_service) with repos and service mocked."""
    analysis_repo = MagicMock()
    analysis_repo.get = AsyncMock(return_value=analysis or _make_analysis())

    category_repo = MagicMock()
    category_repo.get = AsyncMock(return_value=category or _make_category())

    minio = MagicMock()
    report_repo = MagicMock()
    blob_settings = MagicMock()
    blob_settings.bucket_reports = "trendstorm-reports"

    mock_result = PublishResult(
        markdown_report_id=new_id(),
        pdf_report_id=new_id(),
        json_report_id=new_id(),
    )
    mock_service = MagicMock()
    mock_service.publish = AsyncMock(return_value=mock_result)

    pipeline = PublisherPipeline(
        analysis_repo=analysis_repo,
        category_repo=category_repo,
        minio=minio,
        report_repo=report_repo,
        blob_settings=blob_settings,
    )
    # Replace the internal service with our mock
    pipeline._service = mock_service  # type: ignore[attr-defined]

    return pipeline, mock_service


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPublisherPipelineHappyPath:
    async def test_returns_pipeline_result(self) -> None:
        pipeline, _ = _build_pipeline()
        result = await pipeline.process(
            tenant_id="t1", job_id="job-1",
            analysis_id="ana-1", category_id="cat-1",
        )
        assert isinstance(result, PublishPipelineResult)

    async def test_result_carries_job_and_analysis_ids(self) -> None:
        pipeline, _ = _build_pipeline()
        result = await pipeline.process(
            tenant_id="t1", job_id="job-1",
            analysis_id="ana-1", category_id="cat-1",
        )
        assert result.job_id == "job-1"
        assert result.analysis_id == "ana-1"

    async def test_result_carries_publish_result(self) -> None:
        pipeline, _ = _build_pipeline()
        result = await pipeline.process(
            tenant_id="t1", job_id="job-1",
            analysis_id="ana-1", category_id="cat-1",
        )
        assert isinstance(result.result, PublishResult)

    async def test_calls_service_with_analysis_and_category(self) -> None:
        analysis = _make_analysis(tenant_id="t1")
        category = _make_category(tenant_id="t1")
        pipeline, mock_service = _build_pipeline(analysis=analysis, category=category)
        await pipeline.process(
            tenant_id="t1", job_id="job-1",
            analysis_id="ana-1", category_id="cat-1",
        )
        mock_service.publish.assert_called_once_with(analysis, category)

    async def test_fetches_analysis_with_correct_tenant(self) -> None:
        pipeline, _ = _build_pipeline()
        with patch.object(pipeline._analysis_repo, "get", new_callable=AsyncMock,  # type: ignore[attr-defined]
                          return_value=_make_analysis()) as mock_get:
            await pipeline.process(
                tenant_id="tenant-XYZ", job_id="j1",
                analysis_id="ana-1", category_id="cat-1",
            )
        mock_get.assert_called_once_with("tenant-XYZ", "ana-1")

    async def test_fetches_category_with_correct_tenant(self) -> None:
        pipeline, _ = _build_pipeline()
        with patch.object(pipeline._category_repo, "get", new_callable=AsyncMock,  # type: ignore[attr-defined]
                          return_value=_make_category()) as mock_get:
            await pipeline.process(
                tenant_id="tenant-XYZ", job_id="j1",
                analysis_id="ana-1", category_id="cat-99",
            )
        mock_get.assert_called_once_with("tenant-XYZ", "cat-99")


# ---------------------------------------------------------------------------
# Not-found errors
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPublisherPipelineNotFound:
    async def test_raises_not_found_if_analysis_missing(self) -> None:
        pipeline, _ = _build_pipeline(analysis=None)
        pipeline._analysis_repo.get = AsyncMock(return_value=None)  # type: ignore[attr-defined]
        with pytest.raises(NotFoundError, match="analysis"):
            await pipeline.process(
                tenant_id="t1", job_id="job-1",
                analysis_id="missing", category_id="cat-1",
            )

    async def test_raises_not_found_if_category_missing(self) -> None:
        pipeline, _ = _build_pipeline(category=None)
        pipeline._category_repo.get = AsyncMock(return_value=None)  # type: ignore[attr-defined]
        with pytest.raises(NotFoundError, match="category"):
            await pipeline.process(
                tenant_id="t1", job_id="job-1",
                analysis_id="ana-1", category_id="missing",
            )

    async def test_service_not_called_when_analysis_missing(self) -> None:
        pipeline, mock_service = _build_pipeline()
        pipeline._analysis_repo.get = AsyncMock(return_value=None)  # type: ignore[attr-defined]
        with pytest.raises(NotFoundError):
            await pipeline.process(
                tenant_id="t1", job_id="job-1",
                analysis_id="missing", category_id="cat-1",
            )
        mock_service.publish.assert_not_called()

    async def test_service_not_called_when_category_missing(self) -> None:
        pipeline, mock_service = _build_pipeline()
        pipeline._category_repo.get = AsyncMock(return_value=None)  # type: ignore[attr-defined]
        with pytest.raises(NotFoundError):
            await pipeline.process(
                tenant_id="t1", job_id="job-1",
                analysis_id="ana-1", category_id="missing",
            )
        mock_service.publish.assert_not_called()
