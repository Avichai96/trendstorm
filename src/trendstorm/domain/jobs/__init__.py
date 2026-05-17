"""Job domain — models and persistence contracts."""

from trendstorm.domain.jobs.models import Job, JobMetrics
from trendstorm.domain.jobs.repository import JobRepository

__all__ = ["Job", "JobMetrics", "JobRepository"]
