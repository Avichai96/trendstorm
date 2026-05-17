"""Unit tests for agents/scout/pipeline.py.

All infrastructure is replaced with fakes — no Docker required.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trendstorm.agents.scout.fetcher import FetchResult
from trendstorm.agents.scout.parsers import ParseResult
from trendstorm.agents.scout.pipeline import (
    IngestionResult,
    SourceOutcome,
    ingest_sources,
)
from trendstorm.agents.state import DocumentRef
from trendstorm.domain.documents.models import FetchMetadata, RawDocument
from trendstorm.domain.sources.models import Source
from trendstorm.shared.errors import BlobError, FetchError, ParseError
from trendstorm.shared.ids import new_id

# ---------------------------------------------------------------------------
# Fixtures / factories
# ---------------------------------------------------------------------------

def _source(url: str = "https://example.com/article") -> Source:
    return Source(
        tenant_id="t1",
        category_id="cat1",
        url=url,
        label="test",
    )


def _fetch_result(source_id: str, url: str = "https://example.com/article") -> FetchResult:
    return FetchResult(
        source_id=source_id,
        url=url,
        raw_bytes=b"<html>hello world</html>",
        content_type="text/html",
        encoding="utf-8",
        metadata=FetchMetadata(http_status=200, content_type="text/html", bytes_fetched=24),
    )


def _parse_result(text: str = "hello world article") -> ParseResult:
    return ParseResult(
        text=text,
        title="Test Article",
        char_count=len(text),
        word_count=len(text.split()),
    )


def _existing_doc(tenant_id: str, source_id: str, c_hash: str) -> RawDocument:
    return RawDocument(
        id=new_id(),
        tenant_id=tenant_id,
        job_id="job_old",
        category_id="cat1",
        source_id=source_id,
        url="https://example.com/article",
        content_hash=c_hash,
        char_count=100,
        blob_uri_raw="s3://trendstorm-raw/t1/job_old/doc1/raw.html",
    )


def _make_infra(
    *,
    fetch_result: FetchResult | None = None,
    fetch_error: Exception | None = None,
    parse_text: str = "hello world article",
    parse_discovered: tuple[str, ...] = (),
    existing_doc: RawDocument | None = None,
    upload_uri: str = "s3://trendstorm-raw/t1/j1/d1/raw.html",
    blob_error: Exception | None = None,
) -> dict[str, Any]:
    fetcher = MagicMock()
    if fetch_error:
        fetcher.fetch = AsyncMock(side_effect=fetch_error)
    else:
        fetcher.fetch = AsyncMock(return_value=fetch_result)

    raw_doc_repo = MagicMock()
    raw_doc_repo.find_by_content_hash = AsyncMock(return_value=existing_doc)
    raw_doc_repo.insert = AsyncMock()

    source_repo = MagicMock()
    source_repo.update_fetch_status = AsyncMock()

    minio = MagicMock()
    minio.settings = MagicMock()
    minio.settings.bucket_raw = "trendstorm-raw"
    if blob_error:
        minio.upload = AsyncMock(side_effect=blob_error)
    else:
        minio.upload = AsyncMock(return_value=upload_uri)

    return {
        "fetcher": fetcher,
        "raw_doc_repo": raw_doc_repo,
        "source_repo": source_repo,
        "minio": minio,
        "parse_text": parse_text,
        "parse_discovered": parse_discovered,
    }


async def _run(
    sources: list[Source],
    infra: dict[str, Any],
    *,
    job_id: str = "job1",
    tenant_id: str = "t1",
) -> IngestionResult:
    parse_result = ParseResult(
        text=infra["parse_text"],
        title="Title",
        char_count=len(infra["parse_text"]),
        word_count=len(infra["parse_text"].split()),
        discovered_urls=infra["parse_discovered"],
    )
    with patch("trendstorm.agents.scout.pipeline.route", return_value=parse_result):
        return await ingest_sources(
            job_id=job_id,
            tenant_id=tenant_id,
            sources=sources,
            fetcher=infra["fetcher"],
            raw_doc_repo=infra["raw_doc_repo"],
            source_repo=infra["source_repo"],
            minio=infra["minio"],
            concurrency=4,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestEmptySources:
    async def test_returns_empty_result(self) -> None:
        result = await ingest_sources(
            job_id="j", tenant_id="t", sources=[],
            fetcher=MagicMock(), raw_doc_repo=MagicMock(),
            source_repo=MagicMock(), minio=MagicMock(),
        )
        assert result.outcomes == []
        assert result.document_refs == []
        assert result.failed_source_ids == []


@pytest.mark.unit
class TestHappyPath:
    async def test_single_source_created(self) -> None:
        src = _source()
        infra = _make_infra(fetch_result=_fetch_result(src.id))
        result = await _run([src], infra)

        assert len(result.outcomes) == 1
        o = result.outcomes[0]
        assert o.status == "created"
        assert o.source_id == src.id
        assert o.document_ref is not None
        assert o.document_ref.source_id == src.id

    async def test_document_ref_has_content_hash(self) -> None:
        from trendstorm.agents.scout.hashing import content_hash
        src = _source()
        text = "unique article body text"
        infra = _make_infra(fetch_result=_fetch_result(src.id), parse_text=text)
        result = await _run([src], infra)

        expected_hash = content_hash(text)
        assert result.outcomes[0].document_ref.content_hash == expected_hash

    async def test_mongo_insert_called_once(self) -> None:
        src = _source()
        infra = _make_infra(fetch_result=_fetch_result(src.id))
        await _run([src], infra)
        infra["raw_doc_repo"].insert.assert_called_once()

    async def test_source_status_updated_to_ok(self) -> None:
        src = _source()
        infra = _make_infra(fetch_result=_fetch_result(src.id))
        await _run([src], infra)
        infra["source_repo"].update_fetch_status.assert_called_once()
        call_kwargs = infra["source_repo"].update_fetch_status.call_args.kwargs
        assert call_kwargs["status"] == "ok"

    async def test_two_minio_uploads_per_source(self) -> None:
        src = _source()
        infra = _make_infra(fetch_result=_fetch_result(src.id))
        await _run([src], infra)
        assert infra["minio"].upload.call_count == 2


@pytest.mark.unit
class TestDedup:
    async def test_deduped_when_hash_matches(self) -> None:
        from trendstorm.agents.scout.hashing import content_hash
        src = _source()
        text = "article content"
        c_hash = content_hash(text)
        existing = _existing_doc("t1", src.id, c_hash)
        infra = _make_infra(
            fetch_result=_fetch_result(src.id),
            parse_text=text,
            existing_doc=existing,
        )
        result = await _run([src], infra)

        assert result.outcomes[0].status == "deduplicated"
        assert result.deduped_count == 1

    async def test_deduped_returns_existing_doc_ref(self) -> None:
        from trendstorm.agents.scout.hashing import content_hash
        src = _source()
        text = "same content"
        c_hash = content_hash(text)
        existing = _existing_doc("t1", src.id, c_hash)
        infra = _make_infra(
            fetch_result=_fetch_result(src.id),
            parse_text=text,
            existing_doc=existing,
        )
        result = await _run([src], infra)

        ref = result.outcomes[0].document_ref
        assert ref is not None
        assert ref.id == existing.id

    async def test_deduped_skips_minio_upload(self) -> None:
        from trendstorm.agents.scout.hashing import content_hash
        src = _source()
        text = "same content"
        existing = _existing_doc("t1", src.id, content_hash(text))
        infra = _make_infra(
            fetch_result=_fetch_result(src.id),
            parse_text=text,
            existing_doc=existing,
        )
        await _run([src], infra)
        infra["minio"].upload.assert_not_called()


@pytest.mark.unit
class TestFailures:
    async def test_fetch_error_marks_failed(self) -> None:
        src = _source()
        err = FetchError("HTTP 404", code="fetch_error")
        infra = _make_infra(fetch_error=err)
        result = await _run([src], infra)

        assert result.outcomes[0].status == "failed"
        assert result.failed_source_ids == [src.id]

    async def test_fetch_error_updates_source_status(self) -> None:
        src = _source()
        infra = _make_infra(fetch_error=FetchError("timeout"))
        await _run([src], infra)
        infra["source_repo"].update_fetch_status.assert_called_once()

    async def test_parse_error_marks_failed(self) -> None:
        src = _source()
        infra = _make_infra(fetch_result=_fetch_result(src.id))
        parse_exc = ParseError("no content")
        with patch("trendstorm.agents.scout.pipeline.route", side_effect=parse_exc):
            result = await ingest_sources(
                job_id="j", tenant_id="t1", sources=[src],
                fetcher=infra["fetcher"], raw_doc_repo=infra["raw_doc_repo"],
                source_repo=infra["source_repo"], minio=infra["minio"],
            )
        assert result.outcomes[0].status == "failed"

    async def test_blob_error_marks_failed(self) -> None:
        src = _source()
        infra = _make_infra(
            fetch_result=_fetch_result(src.id),
            blob_error=BlobError("upload failed"),
        )
        result = await _run([src], infra)
        assert result.outcomes[0].status == "failed"

    async def test_one_failure_does_not_abort_others(self) -> None:
        src_ok = _source("https://good.com/article")
        src_bad = _source("https://bad.com/article")

        fetch_results: dict[str, Any] = {
            src_ok.url: _fetch_result(src_ok.id, src_ok.url),
            src_bad.url: FetchError("HTTP 500"),
        }

        async def _fetch(url: str, *, source_id: str, tenant_id: str) -> FetchResult:
            r = fetch_results[url]
            if isinstance(r, Exception):
                raise r
            return r

        fetcher = MagicMock()
        fetcher.fetch = _fetch

        raw_doc_repo = MagicMock()
        raw_doc_repo.find_by_content_hash = AsyncMock(return_value=None)
        raw_doc_repo.insert = AsyncMock()
        source_repo = MagicMock()
        source_repo.update_fetch_status = AsyncMock()
        minio = MagicMock()
        minio.settings.bucket_raw = "trendstorm-raw"
        minio.upload = AsyncMock(return_value="s3://b/k")

        parse_result = ParseResult(text="ok text", char_count=7, word_count=2)
        with patch("trendstorm.agents.scout.pipeline.route", return_value=parse_result):
            result = await ingest_sources(
                job_id="j", tenant_id="t1", sources=[src_ok, src_bad],
                fetcher=fetcher, raw_doc_repo=raw_doc_repo,
                source_repo=source_repo, minio=minio, concurrency=2,
            )

        statuses = {o.source_id: o.status for o in result.outcomes}
        assert statuses[src_ok.id] == "created"
        assert statuses[src_bad.id] == "failed"


@pytest.mark.unit
class TestSitemapExpansion:
    async def test_sitemap_queues_discovered_urls(self) -> None:
        src = _source("https://example.com/sitemap.xml")
        discovered = ("https://example.com/a", "https://example.com/b")
        infra = _make_infra(fetch_result=_fetch_result(src.id, src.url))

        # First call (sitemap): returns discovered URLs.
        # Subsequent calls (discovered pages): return plain content.
        _call_count = {"n": 0}
        def _route_side_effect(fetch_result: Any) -> ParseResult:
            _call_count["n"] += 1
            if _call_count["n"] == 1:
                return ParseResult(
                    text="sitemap", char_count=7, word_count=1,
                    discovered_urls=discovered,
                )
            return ParseResult(text="page content", char_count=12, word_count=2)

        with patch("trendstorm.agents.scout.pipeline.route", side_effect=_route_side_effect):
            result = await ingest_sources(
                job_id="j", tenant_id="t1", sources=[src],
                fetcher=infra["fetcher"],
                raw_doc_repo=infra["raw_doc_repo"],
                source_repo=infra["source_repo"],
                minio=infra["minio"],
                concurrency=4,
            )

        # Sitemap source + 2 discovered pages = 3 outcomes total
        assert len(result.outcomes) == 3
        created = [o for o in result.outcomes if o.status == "created"]
        assert len(created) >= 1


@pytest.mark.unit
class TestIngestionResult:
    def test_document_refs_excludes_none(self) -> None:
        r = IngestionResult(outcomes=[
            SourceOutcome(source_id="a", status="created",
                          document_ref=DocumentRef(id="d1", source_id="a",
                                                    content_hash="h1")),
            SourceOutcome(source_id="b", status="failed"),
        ])
        refs = r.document_refs
        assert len(refs) == 1
        assert refs[0].id == "d1"

    def test_failed_source_ids(self) -> None:
        r = IngestionResult(outcomes=[
            SourceOutcome(source_id="a", status="created"),
            SourceOutcome(source_id="b", status="failed"),
            SourceOutcome(source_id="c", status="failed"),
        ])
        assert r.failed_source_ids == ["b", "c"]

    def test_deduped_count(self) -> None:
        r = IngestionResult(outcomes=[
            SourceOutcome(source_id="a", status="created"),
            SourceOutcome(source_id="b", status="deduplicated"),
            SourceOutcome(source_id="c", status="deduplicated"),
        ])
        assert r.deduped_count == 2
