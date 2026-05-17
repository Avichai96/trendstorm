"""MongoDB implementation of RawDocumentRepository."""
from __future__ import annotations

from typing import ClassVar

from trendstorm.domain.documents.models import RawDocument
from trendstorm.infrastructure.mongo.repositories._base import TenantScopedRepository
from trendstorm.infrastructure.mongo.schema import Collection


class MongoRawDocumentRepository(TenantScopedRepository[RawDocument]):
    """Concrete RawDocumentRepository backed by MongoDB."""

    collection: ClassVar[Collection] = Collection.RAW_DOCUMENTS
    model: ClassVar[type[RawDocument]] = RawDocument

    async def insert(self, document: RawDocument) -> None:
        await self._insert(self._encode(document), what=f"RawDocument {document.id}")

    async def get(self, tenant_id: str, document_id: str) -> RawDocument | None:
        doc = await self._find_one(
            self._tenant_query(tenant_id, _id=document_id),
            what=f"RawDocument {document_id}",
        )
        return self._decode(doc) if doc else None

    async def find_by_content_hash(
        self,
        tenant_id: str,
        content_hash: str,
    ) -> RawDocument | None:
        doc = await self._find_one(
            self._tenant_query(tenant_id, content_hash=content_hash),
            what=f"RawDocument hash={content_hash[:12]}…",
        )
        return self._decode(doc) if doc else None

    async def list_by_job(
        self,
        tenant_id: str,
        job_id: str,
    ) -> list[RawDocument]:
        docs = await self._find_many(
            self._tenant_query(tenant_id, job_id=job_id),
            sort=[("_id", 1)],   # natural insertion order, ascending
            what="raw documents by job",
        )
        return [self._decode(d) for d in docs]
