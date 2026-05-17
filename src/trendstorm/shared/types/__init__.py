"""Shared types and enums used across the codebase.

Keep this module dependency-light — it's imported by domain, services, API.
"""
from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    """Lifecycle state of a trend analysis job.

    Transition graph (see Phase 4 LangGraph design):
        PENDING -> INGESTING -> EMBEDDING -> RETRIEVING ->
        ANALYZING -> PUBLISHING -> COMPLETED
        Any state -> FAILED (terminal)
        Any state -> CANCELLED (terminal)
    """

    PENDING = "pending"
    INGESTING = "ingesting"
    EMBEDDING = "embedding"
    RETRIEVING = "retrieving"
    ANALYZING = "analyzing"
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}


class SourceType(StrEnum):
    """Type of data source a user can register."""

    HTTP = "http"          # generic web page
    RSS = "rss"            # RSS/Atom feed
    API = "api"            # arbitrary JSON API
    SITEMAP = "sitemap"    # sitemap.xml crawl


class ReportFormat(StrEnum):
    MARKDOWN = "markdown"
    PDF = "pdf"
    JSON = "json"
