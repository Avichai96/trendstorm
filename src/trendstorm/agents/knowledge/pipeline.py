"""Knowledge pipeline: blob text → parent-child chunks → embeddings → vector store.

Orchestrates:
    1. Idempotency check — skip if chunks already exist for this document.
    2. Text download — fetch extracted text from MinIO via blob_uri_text.
    3. Chunking — ParentChildChunker produces interleaved parent + child RawChunks.
    4. Chunk creation — assign ULIDs, build Chunk domain objects.
    5. Mongo insert — bulk_insert all chunks (ordered=False).
    6. Embedding — embed child texts in max_batch_size-bounded batches.
    7. Vector upsert — write child embeddings to ChromaDB.
    8. Vector-id update — set_vector_id on each child so Mongo reflects the link.

Per-document idempotency (rule 7 in CLAUDE.md architecture):
    If any Chunk already exists for (tenant_id, document_id), the pipeline
    returns immediately with skipped=True. Re-runs are safe.

ChromaDB collection naming (matches VectorStore module docstring):
    f"chunks__{tenant_id[:8].lower()}__{model_id_safe}"
    model_id_safe = model_id with '.' and '-' replaced by '_'
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from trendstorm.domain.chunks.models import Chunk
from trendstorm.infrastructure.blob.uri import parse_s3_uri
from trendstorm.shared.ids import new_id
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.agents.knowledge.chunker import ParentChildChunker
    from trendstorm.domain.chunks.repository import ChunkRepository
    from trendstorm.domain.llm.providers import EmbeddingProvider
    from trendstorm.domain.vectors.store import VectorStore
    from trendstorm.infrastructure.blob.minio_client import MinioClient
    from trendstorm.infrastructure.security.pii import PIIDetector

logger = get_logger(__name__)


@dataclass(frozen=True)
class KnowledgeResult:
    """Outcome of a single document's knowledge pipeline run."""

    document_id: str
    n_chunks_created: int    # total chunks written to Mongo (parents + children)
    n_vectors_upserted: int  # child chunks written to ChromaDB
    skipped: bool            # True if document already had chunks (idempotency hit)


def _collection_name(tenant_id: str, model_id: str) -> str:
    """Build the ChromaDB collection name for this tenant + embedding model."""
    safe = model_id.replace(".", "_").replace("-", "_")
    return f"chunks__{tenant_id[:8].lower()}__{safe}"


class KnowledgePipeline:
    """Orchestrate text chunking, embedding, and vector indexing for one document.

    Injecting all dependencies (chunker, provider, repos, clients) makes this
    fully unit-testable with fakes and reusable across the knowledge worker
    and any future batch-reindex tooling.
    """

    def __init__(
        self,
        chunker: ParentChildChunker,
        embedding_provider: EmbeddingProvider,
        chunk_repo: ChunkRepository,
        vector_store: VectorStore,
        minio: MinioClient,
        pii_detector: PIIDetector | None = None,
    ) -> None:
        self._chunker = chunker
        self._embed = embedding_provider
        self._chunk_repo = chunk_repo
        self._vector_store = vector_store
        self._minio = minio
        self._pii = pii_detector

    async def process_document(
        self,
        *,
        document_id: str,
        blob_uri_text: str,
        tenant_id: str,
        job_id: str,
        category_id: str,
        source_id: str,
    ) -> KnowledgeResult:
        """Run the full pipeline for one document. Idempotent."""
        # ------------------------------------------------------------------
        # 1. Idempotency: skip if already chunked under the current model.
        # Passing embedding_model filters to child chunks only (parents have
        # embedding_model=None). Switching providers returns 0 here, forcing
        # re-embedding; old chunks become orphans and TTL out.
        # ------------------------------------------------------------------
        existing = await self._chunk_repo.list_by_document(
            tenant_id, document_id, embedding_model=self._embed.model_id
        )
        if existing:
            logger.info(
                "knowledge.pipeline.skip",
                document_id=document_id,
                reason="already_chunked",
                n_existing=len(existing),
            )
            return KnowledgeResult(
                document_id=document_id,
                n_chunks_created=0,
                n_vectors_upserted=0,
                skipped=True,
            )

        # ------------------------------------------------------------------
        # 2. Fetch text from MinIO
        # ------------------------------------------------------------------
        bucket, key = parse_s3_uri(blob_uri_text)
        raw_bytes = await self._minio.download(bucket, key)
        text = raw_bytes.decode("utf-8")

        if not text.strip():
            logger.info("knowledge.pipeline.skip", document_id=document_id, reason="empty_text")
            return KnowledgeResult(
                document_id=document_id,
                n_chunks_created=0,
                n_vectors_upserted=0,
                skipped=False,
            )

        # ------------------------------------------------------------------
        # 3. Chunk
        # ------------------------------------------------------------------
        raw_chunks = self._chunker.chunk(text)
        if not raw_chunks:
            return KnowledgeResult(
                document_id=document_id,
                n_chunks_created=0,
                n_vectors_upserted=0,
                skipped=False,
            )

        # ------------------------------------------------------------------
        # 4. Build Chunk domain objects
        # ------------------------------------------------------------------
        # id_by_list_index maps raw_chunks list index → assigned ULID.
        # Parents are guaranteed to appear before their children in raw_chunks.
        id_by_list_index: dict[int, str] = {}
        all_chunks: list[Chunk] = []
        child_chunks: list[Chunk] = []

        for idx, spec in enumerate(raw_chunks):
            cid = new_id()
            id_by_list_index[idx] = cid

            if spec.is_parent:
                chunk = Chunk(
                    id=cid,
                    tenant_id=tenant_id,
                    job_id=job_id,
                    category_id=category_id,
                    document_id=document_id,
                    source_id=source_id,
                    position=spec.position,
                    text=spec.text,
                    token_count=spec.token_count,
                    parent_chunk_id=None,
                    vector_id=None,        # parents are never embedded
                    embedding_model=None,
                    char_start=spec.char_start,
                    char_end=spec.char_end,
                )
            else:
                parent_cid = id_by_list_index[spec.parent_index]  # type: ignore[index]
                chunk = Chunk(
                    id=cid,
                    tenant_id=tenant_id,
                    job_id=job_id,
                    category_id=category_id,
                    document_id=document_id,
                    source_id=source_id,
                    position=spec.position,
                    text=spec.text,
                    token_count=spec.token_count,
                    parent_chunk_id=parent_cid,
                    vector_id=None,        # set in step 8 after successful Chroma write
                    embedding_model=self._embed.model_id,
                    char_start=spec.char_start,
                    char_end=spec.char_end,
                )
                child_chunks.append(chunk)

            all_chunks.append(chunk)

        # ------------------------------------------------------------------
        # 5. Persist all chunks to Mongo
        # ------------------------------------------------------------------
        await self._chunk_repo.bulk_insert(all_chunks)

        if not child_chunks:
            return KnowledgeResult(
                document_id=document_id,
                n_chunks_created=len(all_chunks),
                n_vectors_upserted=0,
                skipped=False,
            )

        # ------------------------------------------------------------------
        # 6. Embed children in batches (respects provider.max_batch_size)
        # PII redaction runs here so private data is never sent to external
        # embedding providers. Original text stays in Mongo (internal store).
        # ------------------------------------------------------------------
        child_texts: list[str] = []
        for child in child_chunks:
            if self._pii is not None:
                result = self._pii.detect_and_redact(child.text)
                if result.has_pii:
                    logger.warning(
                        "knowledge.pii_redacted_before_embed",
                        document_id=document_id,
                        chunk_id=child.id,
                        pii_types=[d.pii_type for d in result.detections],
                    )
                child_texts.append(result.redacted_text)
            else:
                child_texts.append(child.text)
        all_vectors: list[list[float]] = []
        max_batch = self._embed.max_batch_size

        for i in range(0, len(child_texts), max_batch):
            batch = child_texts[i : i + max_batch]
            embed_result = await self._embed.embed_batch(batch)
            all_vectors.extend(embed_result.vectors)

        # ------------------------------------------------------------------
        # 7. Upsert child embeddings to vector store
        # ------------------------------------------------------------------
        collection = _collection_name(tenant_id, self._embed.model_id)
        child_ids = [c.id for c in child_chunks]
        metadatas = [
            {
                "tenant_id": tenant_id,
                "category_id": category_id,
                "document_id": document_id,
                "source_id": source_id,
            }
            for _ in child_chunks
        ]
        await self._vector_store.upsert(
            collection=collection,
            ids=child_ids,
            embeddings=all_vectors,
            documents=child_texts,
            metadatas=metadatas,
        )

        # ------------------------------------------------------------------
        # 8. Record vector_ids in Mongo (closes the chunk lifecycle)
        # ------------------------------------------------------------------
        # vector_id == chunk.id — the same ULID is used as the key in ChromaDB.
        for child_id in child_ids:
            await self._chunk_repo.set_vector_id(
                tenant_id, child_id, child_id, self._embed.model_id
            )

        logger.info(
            "knowledge.pipeline.done",
            document_id=document_id,
            n_chunks=len(all_chunks),
            n_children=len(child_chunks),
            collection=collection,
            model=self._embed.model_id,
        )

        return KnowledgeResult(
            document_id=document_id,
            n_chunks_created=len(all_chunks),
            n_vectors_upserted=len(child_chunks),
            skipped=False,
        )
