"""Mongo-backed repository implementations."""

from trendstorm.infrastructure.mongo.repositories.analysis_repository import (
    MongoAnalysisRepository,
)
from trendstorm.infrastructure.mongo.repositories.api_key_repository import (
    MongoApiKeyRepository,
)
from trendstorm.infrastructure.mongo.repositories.audit_log_repository import (
    MongoAuditLogRepository,
)
from trendstorm.infrastructure.mongo.repositories.category_repository import (
    MongoCategoryRepository,
)
from trendstorm.infrastructure.mongo.repositories.chunk_repository import (
    MongoChunkRepository,
)
from trendstorm.infrastructure.mongo.repositories.idempotency_repository import (
    IdempotencyRepository,
    IdempotencyResult,
)
from trendstorm.infrastructure.mongo.repositories.job_repository import MongoJobRepository
from trendstorm.infrastructure.mongo.repositories.memory_repository import (
    MongoMemoryRepository,
)
from trendstorm.infrastructure.mongo.repositories.raw_document_repository import (
    MongoRawDocumentRepository,
)
from trendstorm.infrastructure.mongo.repositories.report_repository import (
    MongoReportRepository,
)
from trendstorm.infrastructure.mongo.repositories.review_repository import (
    MongoReviewRepository,
)
from trendstorm.infrastructure.mongo.repositories.source_repository import (
    MongoSourceRepository,
)
from trendstorm.infrastructure.mongo.repositories.tenant_repository import (
    MongoTenantRepository,
)
from trendstorm.infrastructure.mongo.repositories.tenant_settings_repository import (
    MongoTenantSettingsRepository,
)
from trendstorm.infrastructure.mongo.repositories.url_blocklist_repository import (
    MongoUrlBlocklistRepository,
)

__all__ = [
    "IdempotencyRepository",
    "IdempotencyResult",
    "MongoAnalysisRepository",
    "MongoApiKeyRepository",
    "MongoAuditLogRepository",
    "MongoCategoryRepository",
    "MongoChunkRepository",
    "MongoJobRepository",
    "MongoMemoryRepository",
    "MongoRawDocumentRepository",
    "MongoReportRepository",
    "MongoReviewRepository",
    "MongoSourceRepository",
    "MongoTenantRepository",
    "MongoTenantSettingsRepository",
    "MongoUrlBlocklistRepository",
]
