"""Publisher service — renders Analysis and writes Reports to MinIO + Mongo.

Renders all three formats (MD/PDF/JSON) in one call. Each gets its own
Report row with the blob_uri so the API can serve download links.

Rendering order: Markdown first (canonical), then derive PDF + JSON.
"""
from __future__ import annotations

from dataclasses import dataclass

from trendstorm.domain.analyses.models import Analysis
from trendstorm.domain.categories.models import Category
from trendstorm.domain.reports.models import Report
from trendstorm.infrastructure.blob.minio_client import MinioClient
from trendstorm.infrastructure.blob.uri import report_key
from trendstorm.infrastructure.mongo.repositories import MongoReportRepository
from trendstorm.services.publish.renderer import RenderEngine
from trendstorm.shared.config import BlobSettings
from trendstorm.shared.ids import new_id
from trendstorm.shared.logging import get_logger
from trendstorm.shared.types import ReportFormat

logger = get_logger(__name__)


@dataclass(frozen=True)
class PublishResult:
    """IDs of the persisted Report rows (one per format)."""

    markdown_report_id: str
    pdf_report_id: str | None   # None if weasyprint fails
    json_report_id: str


class PublisherService:
    """Renders an Analysis to all report formats, uploads to MinIO, inserts Reports."""

    def __init__(
        self,
        renderer: RenderEngine,
        minio: MinioClient,
        report_repo: MongoReportRepository,
        blob_settings: BlobSettings,
    ) -> None:
        self._renderer = renderer
        self._minio = minio
        self._report_repo = report_repo
        self._blob_settings = blob_settings

    async def publish(
        self,
        analysis: Analysis,
        category: Category,
    ) -> PublishResult:
        """Render + upload all three formats.

        Returns PublishResult with IDs for all persisted Report rows.
        Swallows PDF generation failures (weasyprint requires system libs).
        """
        title = _make_title(analysis, category)
        bucket = self._blob_settings.bucket_reports

        # Step 1: Markdown (canonical source of truth).
        md_text = self._renderer.render_markdown(
            analysis, category_name=category.name
        )
        md_id = new_id()
        md_key = report_key(analysis.tenant_id, analysis.job_id, md_id, fmt="md")
        md_uri = await self._minio.upload(bucket, md_key, md_text.encode("utf-8"), content_type="text/markdown")
        md_report = Report(
            id=md_id,
            tenant_id=analysis.tenant_id,
            job_id=analysis.job_id,
            category_id=analysis.category_id,
            analysis_id=analysis.id,
            format=ReportFormat.MARKDOWN,
            blob_uri=md_uri,
            blob_size_bytes=len(md_text.encode("utf-8")),
            title=title,
        )
        await self._report_repo.insert(md_report)

        # Step 2: JSON (always works — pure Python serialization).
        json_bytes = self._renderer.render_json(analysis)
        json_id = new_id()
        json_key = report_key(analysis.tenant_id, analysis.job_id, json_id, fmt="json")
        json_uri = await self._minio.upload(bucket, json_key, json_bytes, content_type="application/json")
        json_report = Report(
            id=json_id,
            tenant_id=analysis.tenant_id,
            job_id=analysis.job_id,
            category_id=analysis.category_id,
            analysis_id=analysis.id,
            format=ReportFormat.JSON,
            blob_uri=json_uri,
            blob_size_bytes=len(json_bytes),
            title=title,
        )
        await self._report_repo.insert(json_report)

        # Step 3: PDF (best-effort — can fail if system libs missing).
        pdf_report_id: str | None = None
        try:
            pdf_bytes = self._renderer.render_pdf(md_text)
            pdf_id = new_id()
            pdf_key = report_key(analysis.tenant_id, analysis.job_id, pdf_id, fmt="pdf")
            pdf_uri = await self._minio.upload(bucket, pdf_key, pdf_bytes, content_type="application/pdf")
            pdf_report = Report(
                id=pdf_id,
                tenant_id=analysis.tenant_id,
                job_id=analysis.job_id,
                category_id=analysis.category_id,
                analysis_id=analysis.id,
                format=ReportFormat.PDF,
                blob_uri=pdf_uri,
                blob_size_bytes=len(pdf_bytes),
                title=title,
            )
            await self._report_repo.insert(pdf_report)
            pdf_report_id = pdf_id
        except Exception as exc:
            logger.warning(
                "pdf_render_failed",
                job_id=analysis.job_id,
                error=str(exc),
            )

        logger.info(
            "reports_published",
            job_id=analysis.job_id,
            analysis_id=analysis.id,
            md_id=md_id,
            json_id=json_id,
            pdf_id=pdf_report_id,
        )

        return PublishResult(
            markdown_report_id=md_id,
            pdf_report_id=pdf_report_id,
            json_report_id=json_id,
        )


def _make_title(analysis: Analysis, category: Category) -> str:
    """Generate a human-friendly report title."""
    summary_preview = analysis.summary[:60].rstrip()
    if len(analysis.summary) > 60:
        summary_preview += "…"
    return f"{category.name}: {summary_preview}"
