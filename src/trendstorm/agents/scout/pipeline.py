"""Scout ingestion pipeline.

Composes Fetcher → Parser → Dedup → MinIO upload → Mongo write for a list of
Sources belonging to a single job. Uses a producer-consumer queue so slow or
rate-limited hosts don't block all other work, and concurrency is bounded.

Each source is processed independently; a failure on one source does not abort
the rest. Partial success (some sources failed) is acceptable — the caller
decides whether to proceed or retry based on the returned IngestionResult.

Usage (from the Scout Kafka worker):
    result = await ingest_sources(
        job_id=event.job_id,
        tenant_id=event.tenant_id,
        sources=sources,
        fetcher=fetcher,
        raw_doc_repo=raw_doc_repo,
        source_repo=source_repo,
        minio=minio,
        concurrency=settings.ingest.concurrency_per_job,
    )
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from opentelemetry import trace

from trendstorm.agents.scout.hashing import content_hash
from trendstorm.agents.scout.parsers import route
from trendstorm.domain.documents.models import RawDocument
from trendstorm.infrastructure.blob.uri import raw_key, text_key
from trendstorm.infrastructure.mongo.repositories._base import now_utc
from trendstorm.shared.errors import (
    BlobError,
    ConflictError,
    FetchError,
    ParseError,
    TrendStormError,
)
from trendstorm.shared.ids import new_id
from trendstorm.shared.logging import get_logger
from trendstorm.shared.types import SourceType

if TYPE_CHECKING:
    from trendstorm.agents.scout.fetcher import Fetcher
    from trendstorm.agents.state import DocumentRef
    from trendstorm.domain.sources.models import Source
    from trendstorm.infrastructure.blob.minio_client import MinioClient
    from trendstorm.infrastructure.mongo.repositories.raw_document_repository import (
        MongoRawDocumentRepository,
    )
    from trendstorm.infrastructure.mongo.repositories.source_repository import (
        MongoSourceRepository,
    )

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)


# ---------------------------------------------------------------------------
# Internal task type — one entry per URL in the work queue
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _FetchTask:
    url: str
    source_id: str
    category_id: str
    source_type: str            # SourceType.value
    update_source_status: bool  # False for sitemap-discovered sub-URLs


# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SourceOutcome:
    """Result of processing one source (or one sitemap-discovered URL)."""

    source_id: str
    status: Literal["created", "deduplicated", "failed"]
    document_ref: DocumentRef | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class IngestionResult:
    """Aggregated result for the whole job's ingestion pass."""

    outcomes: list[SourceOutcome] = field(default_factory=list)

    @property
    def document_refs(self) -> list[DocumentRef]:
        return [o.document_ref for o in self.outcomes if o.document_ref is not None]

    @property
    def failed_source_ids(self) -> list[str]:
        return [o.source_id for o in self.outcomes if o.status == "failed"]

    @property
    def deduped_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "deduplicated")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def ingest_sources(
    *,
    job_id: str,
    tenant_id: str,
    sources: list[Source],
    fetcher: Fetcher,
    raw_doc_repo: MongoRawDocumentRepository,
    source_repo: MongoSourceRepository,
    minio: MinioClient,
    concurrency: int = 16,
) -> IngestionResult:
    """Fetch, parse, dedup, and store all sources for a job.

    Returns IngestionResult even when some (or all) sources fail; the caller
    checks failed_source_ids and decides whether to emit a completion event
    or route to a retry topic.
    """
    if not sources:
        return IngestionResult()

    queue: asyncio.Queue[_FetchTask] = asyncio.Queue()
    result = IngestionResult()

    for source in sources:
        queue.put_nowait(
            _FetchTask(
                url=source.url,
                source_id=source.id,
                category_id=source.category_id,
                source_type=source.type.value,
                update_source_status=True,
            )
        )

    ctx = _PipelineContext(
        job_id=job_id,
        tenant_id=tenant_id,
        fetcher=fetcher,
        raw_doc_repo=raw_doc_repo,
        source_repo=source_repo,
        minio=minio,
    )

    async def worker() -> None:
        while True:
            task = await queue.get()
            try:
                outcome, extra_tasks = await _process_task(task, ctx)
                result.outcomes.append(outcome)
                for extra in extra_tasks:
                    await queue.put(extra)
            except Exception:  # belt-and-suspenders; _process_task must not raise
                logger.exception("pipeline_worker_unhandled", source_id=task.source_id)
                result.outcomes.append(
                    SourceOutcome(
                        source_id=task.source_id,
                        status="failed",
                        error_code="internal_error",
                        error_message="Unexpected pipeline error",
                    )
                )
            finally:
                queue.task_done()

    n = min(len(sources), concurrency)
    worker_tasks = [asyncio.create_task(worker()) for _ in range(n)]

    await queue.join()

    for t in worker_tasks:
        t.cancel()
    await asyncio.gather(*worker_tasks, return_exceptions=True)

    logger.info(
        "ingest_pipeline_done",
        job_id=job_id,
        total=len(result.outcomes),
        created=sum(1 for o in result.outcomes if o.status == "created"),
        deduped=result.deduped_count,
        failed=len(result.failed_source_ids),
    )
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _PipelineContext:
    """Bundle of shared clients passed to every task processor."""

    job_id: str
    tenant_id: str
    fetcher: Fetcher
    raw_doc_repo: MongoRawDocumentRepository
    source_repo: MongoSourceRepository
    minio: MinioClient


async def _process_task(
    task: _FetchTask,
    ctx: _PipelineContext,
) -> tuple[SourceOutcome, list[_FetchTask]]:
    """Process one URL: fetch → parse → dedup → upload → write. Never raises."""
    with tracer.start_as_current_span(
        "scout.ingest_source",
        attributes={
            "trendstorm.source_id": task.source_id,
            "trendstorm.job_id": ctx.job_id,
            "trendstorm.source_type": task.source_type,
        },
    ):
        return await _process_task_inner(task, ctx)


async def _process_task_inner(
    task: _FetchTask,
    ctx: _PipelineContext,
) -> tuple[SourceOutcome, list[_FetchTask]]:
    # 1. Fetch ----------------------------------------------------------------
    try:
        fetch_result = await ctx.fetcher.fetch(
            task.url, source_id=task.source_id, tenant_id=ctx.tenant_id
        )
    except FetchError as exc:
        await _update_status(ctx.source_repo, ctx.tenant_id, task.source_id,
                              status=exc.code, error=exc.message,
                              update=task.update_source_status)
        return SourceOutcome(
            source_id=task.source_id, status="failed",
            error_code=exc.code, error_message=exc.message,
        ), []

    # 2. Parse ----------------------------------------------------------------
    try:
        parse_result = route(fetch_result)
    except ParseError as exc:
        await _update_status(ctx.source_repo, ctx.tenant_id, task.source_id,
                              status=exc.code, error=exc.message,
                              update=task.update_source_status)
        return SourceOutcome(
            source_id=task.source_id, status="failed",
            error_code=exc.code, error_message=exc.message,
        ), []

    # 3. Sitemap expansion — queue discovered URLs, no document stored here --
    if parse_result.discovered_urls:
        extra = [
            _FetchTask(
                url=url,
                source_id=task.source_id,
                category_id=task.category_id,
                source_type=SourceType.HTTP.value,
                update_source_status=False,
            )
            for url in parse_result.discovered_urls
        ]
        await _update_status(ctx.source_repo, ctx.tenant_id, task.source_id,
                              status="ok", update=task.update_source_status)
        logger.info("sitemap_expanded", source_id=task.source_id, count=len(extra))
        return SourceOutcome(source_id=task.source_id, status="created"), extra

    # 4. Content dedup --------------------------------------------------------
    c_hash = content_hash(parse_result.text)
    existing = await ctx.raw_doc_repo.find_by_content_hash(ctx.tenant_id, c_hash)
    if existing:
        doc_ref = _make_ref(existing.id, task.source_id, c_hash,
                            existing.blob_uri_raw, existing.char_count)
        await _update_status(ctx.source_repo, ctx.tenant_id, task.source_id,
                              status="ok", update=task.update_source_status)
        logger.debug("dedup_hit", source_id=task.source_id, doc_id=existing.id)
        return SourceOutcome(
            source_id=task.source_id, status="deduplicated", document_ref=doc_ref
        ), []

    # 5. Upload to blob store -------------------------------------------------
    doc_id = new_id()
    try:
        uri_raw = await ctx.minio.upload(
            ctx.minio.settings.bucket_raw,
            raw_key(ctx.tenant_id, ctx.job_id, doc_id),
            fetch_result.raw_bytes,
            content_type=fetch_result.content_type,
        )
        uri_text = await ctx.minio.upload(
            ctx.minio.settings.bucket_raw,
            text_key(ctx.tenant_id, ctx.job_id, doc_id),
            parse_result.text.encode("utf-8"),
            content_type="text/plain; charset=utf-8",
        )
    except BlobError as exc:
        await _update_status(ctx.source_repo, ctx.tenant_id, task.source_id,
                              status=exc.code, error=exc.message,
                              update=task.update_source_status)
        return SourceOutcome(
            source_id=task.source_id, status="failed",
            error_code=exc.code, error_message=exc.message,
        ), []

    # 6. Write RawDocument to Mongo -------------------------------------------
    raw_doc = RawDocument(
        id=doc_id,
        tenant_id=ctx.tenant_id,
        job_id=ctx.job_id,
        category_id=task.category_id,
        source_id=task.source_id,
        url=fetch_result.url,
        content_hash=c_hash,
        char_count=len(parse_result.text),
        word_count=parse_result.word_count,
        blob_uri_raw=uri_raw,
        blob_uri_text=uri_text,
        language=parse_result.language,
        title=parse_result.title,
        fetch_metadata=fetch_result.metadata,
        extracted_at=now_utc(),
    )
    try:
        await ctx.raw_doc_repo.insert(raw_doc)
    except ConflictError:
        # Race: another worker inserted the same content_hash between our dedup
        # check and our insert. Treat as a dedup — the doc already exists.
        logger.debug("dedup_race", source_id=task.source_id, hash=c_hash[:12])
        await _update_status(ctx.source_repo, ctx.tenant_id, task.source_id,
                              status="ok", update=task.update_source_status)
        return SourceOutcome(source_id=task.source_id, status="deduplicated"), []

    # 7. Update source fetch status -------------------------------------------
    await _update_status(ctx.source_repo, ctx.tenant_id, task.source_id,
                         status="ok", update=task.update_source_status)

    doc_ref = _make_ref(doc_id, task.source_id, c_hash, uri_raw, raw_doc.char_count)
    logger.info("document_created", source_id=task.source_id, doc_id=doc_id,
                char_count=raw_doc.char_count)
    return SourceOutcome(
        source_id=task.source_id, status="created", document_ref=doc_ref
    ), []


def _make_ref(
    doc_id: str,
    source_id: str,
    c_hash: str,
    blob_uri: str | None,
    char_count: int,
) -> DocumentRef:
    from trendstorm.agents.state import DocumentRef  # avoid circular at module load
    return DocumentRef(
        id=doc_id,
        source_id=source_id,
        content_hash=c_hash,
        blob_uri=blob_uri,
        char_count=char_count,
    )


async def _update_status(
    repo: MongoSourceRepository,
    tenant_id: str,
    source_id: str,
    *,
    status: str,
    error: str | None = None,
    update: bool = True,
) -> None:
    """Update source fetch status. Swallows errors — status is eventually consistent."""
    if not update:
        return
    try:
        await repo.update_fetch_status(
            tenant_id, source_id,
            status=status,
            error=error,
            fetched_at=now_utc(),
        )
    except TrendStormError:
        logger.warning("source_status_update_failed", source_id=source_id, status=status)
