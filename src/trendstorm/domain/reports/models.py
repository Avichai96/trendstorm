"""Report domain model.

A Report is the FINAL, USER-FACING artifact produced from an Analysis:
a rendered Markdown / PDF / JSON file uploaded to MinIO. This model
stores the metadata + the download link, not the content.

Why a separate model from Analysis?
    - One Analysis can render to multiple Report formats (md, pdf, json).
    - Reports are immutable once published; Analyses can be re-validated.
    - The download surface is simpler when each blob has its own row.

Lifecycle:
    1. Analysis insert (validator_passed=True)
    2. Publisher renders to chosen format(s) -> uploads to MinIO
    3. Insert one Report row per format with blob_uri
    4. API responds with Report blob URIs for download
"""
from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from trendstorm.shared.ids import new_id
from trendstorm.shared.types import ReportFormat


class Report(BaseModel):
    """A published report file."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    tenant_id: str
    job_id: str
    category_id: str
    analysis_id: str            # which analysis was rendered

    format: ReportFormat
    blob_uri: str               # "s3://trendstorm-reports/{tenant}/{job}/{id}.pdf"
    blob_size_bytes: int = 0

    # Human-friendly. The renderer chooses a title from the analysis summary.
    title: str = Field(..., min_length=1, max_length=300)

    # If we later support summarized "executive previews," this is where
    # they'd live; for Phase 5 we just record the file ref.

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
