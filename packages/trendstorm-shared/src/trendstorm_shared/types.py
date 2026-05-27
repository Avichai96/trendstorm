"""Shared enumeration types — the wire-format vocabulary for TrendStorm AI.

These must match the server's `shared/types/__init__.py` and domain enums exactly.
When the server adds a new status, add it here too before or alongside the deploy.
"""
from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    PENDING = "pending"
    INGESTING = "ingesting"
    EMBEDDING = "embedding"
    RETRIEVING = "retrieving"
    ANALYZING = "analyzing"
    AWAITING_REVIEW = "awaiting_review"
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.REJECTED,
        }


class SourceType(StrEnum):
    HTTP = "http"
    RSS = "rss"
    API = "api"
    SITEMAP = "sitemap"


class ReportFormat(StrEnum):
    MARKDOWN = "markdown"
    PDF = "pdf"
    JSON = "json"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REFINEMENT_REQUESTED = "refinement_requested"
    TIMED_OUT = "timed_out"

    @property
    def is_resolved(self) -> bool:
        return self != ReviewStatus.PENDING


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_REFINEMENT = "request_refinement"


class StreamEventType(StrEnum):
    STAGE_STARTED = "stage_started"
    STAGE_COMPLETED = "stage_completed"
    STAGE_FAILED = "stage_failed"
    PROGRESS = "progress"
    PARTIAL_TEXT = "partial_text"
    CITATION_ADDED = "citation_added"
    REPORT_READY = "report_ready"
    JOB_FAILED = "job_failed"
    JOB_REJECTED = "job_rejected"
    REVIEW_REQUIRED = "review_required"
    REVIEW_RESOLVED = "review_resolved"
    HEARTBEAT = "heartbeat"

    @property
    def is_terminal(self) -> bool:
        return self in {
            StreamEventType.REPORT_READY,
            StreamEventType.JOB_FAILED,
            StreamEventType.JOB_REJECTED,
        }
