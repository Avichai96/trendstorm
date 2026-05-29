"""Publisher pipeline — thin orchestration wrapper for the worker.

Loads the Analysis and Category from Mongo, delegates rendering and upload
to PublisherService, and returns the PublishResult. Separates I/O wiring
(repositories, config) from rendering logic (PublisherService).
"""

from __future__ import annotations

from dataclasses import dataclass

from trendstorm.infrastructure.blob.minio_client import MinioClient
from trendstorm.infrastructure.mongo.repositories import (
    MongoAnalysisRepository,
    MongoCategoryRepository,
    MongoReportRepository,
)
from trendstorm.services.publish.renderer import RenderEngine
from trendstorm.services.publish.service import PublisherService, PublishResult
from trendstorm.shared.config import BlobSettings
from trendstorm.shared.errors import NotFoundError
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class PublishPipelineResult:
    """Outcome of a publish pipeline run."""

    job_id: str
    analysis_id: str
    result: PublishResult


class PublisherPipeline:
    """Loads domain objects and delegates to PublisherService."""

    def __init__(
        self,
        analysis_repo: MongoAnalysisRepository,
        category_repo: MongoCategoryRepository,
        minio: MinioClient,
        report_repo: MongoReportRepository,
        blob_settings: BlobSettings,
    ) -> None:
        self._analysis_repo = analysis_repo
        self._category_repo = category_repo
        self._service = PublisherService(
            renderer=RenderEngine(),
            minio=minio,
            report_repo=report_repo,
            blob_settings=blob_settings,
        )

    async def process(
        self,
        *,
        tenant_id: str,
        job_id: str,
        analysis_id: str,
        category_id: str,
    ) -> PublishPipelineResult:
        """Load analysis + category, render all formats, persist Reports.

        Raises NotFoundError if either the analysis or category is missing.
        """
        analysis = await self._analysis_repo.get(tenant_id, analysis_id)
        if analysis is None:
            raise NotFoundError(f"analysis {analysis_id!r} not found for tenant {tenant_id!r}")

        category = await self._category_repo.get(tenant_id, category_id)
        if category is None:
            raise NotFoundError(f"category {category_id!r} not found for tenant {tenant_id!r}")

        logger.info(
            "publisher_pipeline_started",
            job_id=job_id,
            analysis_id=analysis_id,
            category_id=category_id,
        )

        result = await self._service.publish(analysis, category)

        logger.info(
            "publisher_pipeline_completed",
            job_id=job_id,
            analysis_id=analysis_id,
            md_id=result.markdown_report_id,
            json_id=result.json_report_id,
            pdf_id=result.pdf_report_id,
        )

        return PublishPipelineResult(
            job_id=job_id,
            analysis_id=analysis_id,
            result=result,
        )
