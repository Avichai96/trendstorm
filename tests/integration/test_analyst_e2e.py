"""Integration test: AnalysisPendingEvent → AnalystWorker → Mongo + AnalysisCompletedEvent.

This is the FIRST full-pipeline test that exercises the entire Phase 8 stack:
    1. Insert a small AI-safety corpus into Mongo + ChromaDB (categories,
       documents, chunks, vectors).
    2. Spawn AnalystWorker with real HybridRetriever, real LLM chat provider,
       real validator, optional Cohere reranker.
    3. Publish AnalysisPendingEvent to Kafka.
    4. Consume AnalysisCompletedEvent and inspect the persisted Analysis.

Skip semantics — multi-layer:
    - Infrastructure unavailable (Mongo/Kafka/Chroma) → skip.
    - No chat-provider API key (Anthropic / Gemini / OpenAI) → skip.
    - No embedding-provider API key (Gemini / Ollama unreachable) → skip.

This test is SLOW (60-180s) due to multiple LLM round-trips: query expansion,
analyst tool-use, validator tool-use. Mark @pytest.mark.slow so it does not
run in the fast unit suite. Run manually:

    uv run pytest tests/integration/test_analyst_e2e.py -m "integration and slow" -s
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from aiokafka import AIOKafkaConsumer

from trendstorm.domain.analyses.models import Analysis
from trendstorm.domain.categories.models import Category
from trendstorm.domain.chunks.models import Chunk
from trendstorm.domain.documents.models import RawDocument
from trendstorm.domain.llm.errors import LLMPermanentError
from trendstorm.infrastructure.kafka.producer import KafkaProducerClient
from trendstorm.infrastructure.mongo.client import MongoClient
from trendstorm.infrastructure.mongo.repositories import (
    IdempotencyRepository,
    MongoAnalysisRepository,
    MongoCategoryRepository,
    MongoChunkRepository,
    MongoRawDocumentRepository,
)
from trendstorm.infrastructure.mongo.schema import Collection
from trendstorm.infrastructure.retrieval.chroma_vector import (
    ChromaVectorRetriever,
    _collection_name,
)
from trendstorm.infrastructure.retrieval.cohere_reranker import CohereReranker
from trendstorm.infrastructure.retrieval.mongo_bm25 import MongoBM25Retriever
from trendstorm.infrastructure.vectors.chroma_store import ChromaVectorStore
from trendstorm.orchestration.events import (
    AnalysisCompletedEvent,
    AnalysisPendingEvent,
)
from trendstorm.orchestration.topics import Topic
from trendstorm.orchestration.workers.analyst_worker import AnalystWorker
from trendstorm.services.analysis.analyst import Analyst
from trendstorm.services.analysis.validator import AnalysisValidator
from trendstorm.services.retrieval.hybrid import HybridRetriever
from trendstorm.services.retrieval.query_expansion import QueryExpander
from trendstorm.shared.config import get_settings
from trendstorm.shared.ids import new_id

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# AI-safety fixture corpus — 5 topics, each with 1 parent + 2 child chunks
# ---------------------------------------------------------------------------

_CORPUS = [
    {
        "topic": "rlhf",
        "parent": (
            "Reinforcement Learning from Human Feedback (RLHF) is the dominant "
            "alignment technique for production large language models in 2025. "
            "OpenAI, Anthropic, and Google all use variants. The standard pipeline "
            "has three stages: supervised fine-tuning on human-written examples, "
            "training a reward model from human preference comparisons, and PPO "
            "optimization against that reward model."
        ),
        "child1": "RLHF is the dominant LLM alignment technique used by OpenAI, Anthropic, and Google in 2025.",
        "child2": "The three-stage RLHF pipeline: supervised fine-tuning, preference reward modeling, PPO optimization.",
    },
    {
        "topic": "cai",
        "parent": (
            "Constitutional AI (CAI) is Anthropic's alternative to pure RLHF. "
            "Instead of relying solely on human labels, CAI uses a written "
            "constitution — a set of principles — and lets the model critique "
            "and revise its own outputs against those principles. This reduces "
            "the human-labeling burden and provides clearer transparency about "
            "what behavior is being optimized."
        ),
        "child1": "Constitutional AI lets the model critique and revise its outputs against a written set of principles.",
        "child2": "Anthropic uses Constitutional AI to reduce human-labeling burden and improve transparency over RLHF.",
    },
    {
        "topic": "interp",
        "parent": (
            "Mechanistic interpretability has become a major safety research "
            "direction. Recent work decomposes neural network activations into "
            "interpretable features using sparse autoencoders. This lets researchers "
            "identify specific circuits responsible for behaviors like deception "
            "or sycophancy and intervene on them directly."
        ),
        "child1": "Sparse autoencoders decompose LLM activations into interpretable features researchers can inspect.",
        "child2": "Mechanistic interpretability identifies neural circuits for deception or sycophancy and enables interventions.",
    },
    {
        "topic": "evals",
        "parent": (
            "Capability evaluations have shifted from static benchmarks toward "
            "agentic, multi-step task suites. METR, Apollo Research, and the UK "
            "AISI now run evals testing whether frontier models can autonomously "
            "complete days-long software engineering and research tasks. Results "
            "directly inform deployment decisions at major labs."
        ),
        "child1": "Frontier model evaluations now test agentic multi-step tasks rather than static QA benchmarks.",
        "child2": "METR, Apollo, and the UK AISI run capability evals that inform deployment decisions at frontier labs.",
    },
    {
        "topic": "compute",
        "parent": (
            "Total compute used for frontier LLM training has grown roughly 4x "
            "per year through 2024. The largest training runs in 2025 are "
            "estimated at over 1e26 FLOP. This trajectory has begun to bump "
            "against energy infrastructure constraints and chip supply limits, "
            "leading to slower scaling and more emphasis on data and algorithmic "
            "efficiency."
        ),
        "child1": "Frontier LLM training compute has grown about 4x per year through 2024 and now exceeds 1e26 FLOP.",
        "child2": "Energy and chip supply constraints are slowing pure compute scaling and shifting focus to efficiency.",
    },
]

_QUERY = (
    "What are the current trends in AI alignment and safety research as of 2025? "
    "Focus on techniques used at frontier labs."
)


# ---------------------------------------------------------------------------
# Fixture: stack
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


async def _select_chat_provider(settings: Any):
    """Resolve a chat provider: prefer Anthropic, fall back to Gemini, then OpenAI.
    Skips the test if no key is available.
    """
    anthropic_key = settings.llm.anthropic_api_key.get_secret_value()
    if anthropic_key:
        from trendstorm.infrastructure.llm.anthropic import AnthropicChatProvider
        return AnthropicChatProvider(
            api_key=anthropic_key,
            model="claude-haiku-4-5-20251001",   # Haiku for cost in tests
        )
    gemini_key = settings.gemini.api_key.get_secret_value()
    if gemini_key:
        from trendstorm.infrastructure.llm.gemini import GeminiChatProvider
        return GeminiChatProvider(api_key=gemini_key, model=settings.gemini.chat_model)
    openai_key = settings.llm.openai_api_key.get_secret_value()
    if openai_key:
        from trendstorm.infrastructure.llm.openai import OpenAIChatProvider
        return OpenAIChatProvider(api_key=openai_key, model="gpt-4o-mini")
    return None


async def _select_embedder(settings: Any):
    """Resolve an embedding provider: prefer Gemini (free tier), fall back to Ollama."""
    gemini_key = settings.gemini.api_key.get_secret_value()
    if gemini_key:
        from trendstorm.infrastructure.llm.gemini import GeminiEmbeddingProvider
        return GeminiEmbeddingProvider(api_key=gemini_key, model=settings.gemini.embedding_model)

    try:
        from trendstorm.infrastructure.llm.ollama import OllamaEmbeddingProvider
        ep = OllamaEmbeddingProvider(
            host=settings.llm.ollama_base_url,
            model=settings.llm.ollama_embedding_model,
        )
        await ep.embed_batch(["probe"])
        return ep
    except (LLMPermanentError, Exception):
        return None


@pytest.fixture
async def stack():
    """Spawn a full Analyst stack: infra + corpus + worker.

    Skips at the first missing piece (infra unreachable, no LLM keys).
    """
    settings = get_settings()
    mongo = MongoClient(settings.mongo)
    chroma = ChromaVectorStore(settings.vector)
    producer = KafkaProducerClient(settings.kafka)

    try:
        await asyncio.gather(mongo.connect(), chroma.connect(), producer.start())
    except Exception as exc:
        pytest.skip(f"Infrastructure not reachable: {exc}")

    embedder = await _select_embedder(settings)
    if embedder is None:
        await asyncio.gather(mongo.close(), chroma.close(), producer.stop())
        pytest.skip("No embedding provider available (set GEMINI__API_KEY or run Ollama)")

    chat = await _select_chat_provider(settings)
    if chat is None:
        await asyncio.gather(mongo.close(), chroma.close(), producer.stop())
        pytest.skip(
            "No chat provider key available "
            "(set one of LLM__ANTHROPIC_API_KEY, GEMINI__API_KEY, LLM__OPENAI_API_KEY)"
        )

    # Optional Cohere reranker
    cohere_key = settings.llm.cohere_api_key.get_secret_value()
    reranker: CohereReranker | None = None
    if cohere_key:
        reranker = CohereReranker(api_key=cohere_key, model=settings.llm.cohere_rerank_model)
        await reranker.connect()

    # Insert corpus
    tenant_id = new_id()
    category_id = new_id()
    job_id = new_id()
    source_id = new_id()

    category_repo = MongoCategoryRepository(mongo)
    doc_repo = MongoRawDocumentRepository(mongo)
    chunk_repo = MongoChunkRepository(mongo)
    analysis_repo = MongoAnalysisRepository(mongo)
    idem = IdempotencyRepository(mongo)

    category = Category(
        id=category_id,
        tenant_id=tenant_id,
        name="AI Safety",
        description="Frontier-lab AI alignment and safety research trends in 2025.",
        keywords=["AI safety", "alignment", "RLHF", "interpretability"],
    )
    await category_repo.insert(category)

    # One document per corpus topic; one parent + two child chunks each
    inserted_chunk_ids: list[str] = []
    inserted_doc_ids: list[str] = []
    parent_lookup: dict[str, str] = {}

    for topic_data in _CORPUS:
        doc_id = new_id()
        inserted_doc_ids.append(doc_id)
        document = RawDocument(
            id=doc_id,
            tenant_id=tenant_id,
            job_id=job_id,
            source_id=source_id,
            content_hash=f"hash-{topic_data['topic']}",
            char_count=len(topic_data["parent"]),
        )
        await doc_repo.insert(document)

        parent_id = new_id()
        parent_lookup[topic_data["topic"]] = parent_id
        parent = Chunk(
            id=parent_id, tenant_id=tenant_id, job_id=job_id, category_id=category_id,
            document_id=doc_id, source_id=source_id, position=0,
            text=topic_data["parent"],
        )
        child1 = Chunk(
            tenant_id=tenant_id, job_id=job_id, category_id=category_id,
            document_id=doc_id, source_id=source_id, position=1,
            text=topic_data["child1"], parent_chunk_id=parent_id,
        )
        child2 = Chunk(
            tenant_id=tenant_id, job_id=job_id, category_id=category_id,
            document_id=doc_id, source_id=source_id, position=2,
            text=topic_data["child2"], parent_chunk_id=parent_id,
        )
        await chunk_repo.bulk_insert([parent, child1, child2])
        inserted_chunk_ids.extend([parent_id, child1.id, child2.id])

        # Embed and upsert child chunks (parents aren't embedded by design)
        embed_result = await embedder.embed_batch([child1.text, child2.text])
        collection = _collection_name(tenant_id, embedder.model_id)
        await chroma.upsert(
            collection,
            ids=[child1.id, child2.id],
            embeddings=embed_result.vectors,
            documents=[child1.text, child2.text],
            metadatas=[
                {
                    "tenant_id": tenant_id, "category_id": category_id,
                    "document_id": doc_id, "source_id": source_id,
                },
            ] * 2,
        )
        # Stamp vector_id on the children so retrieval can cross-reference.
        await chunk_repo.set_vector_id(tenant_id, child1.id, child1.id, embedder.model_id)
        await chunk_repo.set_vector_id(tenant_id, child2.id, child2.id, embedder.model_id)

    # Build the Analyst stack
    bm25 = MongoBM25Retriever(mongo)
    vector = ChromaVectorRetriever(chroma, embedder)
    expander = QueryExpander(chat)
    hybrid = HybridRetriever(
        bm25=bm25, vector=vector, expander=expander, mongo=mongo,
        settings=settings.analysis, reranker=reranker,
    )
    validator = AnalysisValidator(chat, settings.analysis)
    analyst = Analyst(hybrid, chat, validator, settings.analysis)

    worker = AnalystWorker(
        kafka_settings=settings.kafka,
        analyst=analyst,
        analysis_repo=analysis_repo,
        category_repo=category_repo,
        idempotency=idem,
        producer=producer,
    )
    await worker.start()
    worker_task = asyncio.create_task(worker.run())

    yield {
        "settings": settings,
        "mongo": mongo,
        "chroma": chroma,
        "producer": producer,
        "embedder": embedder,
        "tenant_id": tenant_id,
        "category_id": category_id,
        "job_id": job_id,
        "category": category,
        "analysis_repo": analysis_repo,
        "chunk_ids": inserted_chunk_ids,
        "doc_ids": inserted_doc_ids,
    }

    # Teardown
    await worker.stop()
    worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker_task
    if reranker:
        await reranker.close()

    # Best-effort cleanup of inserted documents
    try:
        coll_chunks = mongo.db[Collection.CHUNKS.value]
        coll_docs = mongo.db[Collection.RAW_DOCUMENTS.value]
        coll_cats = mongo.db[Collection.CATEGORIES.value]
        coll_analyses = mongo.db[Collection.ANALYSES.value]
        await coll_chunks.delete_many({"_id": {"$in": inserted_chunk_ids}})
        await coll_docs.delete_many({"_id": {"$in": inserted_doc_ids}})
        await coll_cats.delete_many({"_id": category_id})
        await coll_analyses.delete_many({"tenant_id": tenant_id})
        collection = _collection_name(tenant_id, embedder.model_id)
        with contextlib.suppress(Exception):
            await chroma.delete_by_filter(collection, {"tenant_id": {"$eq": tenant_id}})
    except Exception:
        pass

    await asyncio.gather(mongo.close(), chroma.close(), producer.stop())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _publish_analysis_pending(
    producer: KafkaProducerClient,
    *,
    tenant_id: str,
    job_id: str,
    category_id: str,
    refinement_loop: int = 0,
    refinement_notes: str | None = None,
) -> None:
    event = AnalysisPendingEvent(
        correlation_id=new_id(),
        tenant_id=tenant_id,
        job_id=job_id,
        category_id=category_id,
        refinement_loop=refinement_loop,
        refinement_notes=refinement_notes,
    )
    await producer.producer.send_and_wait(
        Topic.ANALYSIS_PENDING.value,
        value=event.model_dump_json().encode(),
        key=job_id.encode(),
    )


async def _consume_analysis_completed(
    settings: Any,
    tenant_id: str,
    job_id: str,
    *,
    time_limit: float = 180.0,
) -> AnalysisCompletedEvent:
    consumer = AIOKafkaConsumer(
        Topic.ANALYSIS_COMPLETED.value,
        bootstrap_servers=settings.kafka.bootstrap_servers,
        group_id=f"test-analyst-{new_id()}",
        auto_offset_reset="earliest",
        enable_auto_commit=True,
    )
    await consumer.start()
    try:
        deadline = asyncio.get_event_loop().time() + time_limit
        while asyncio.get_event_loop().time() < deadline:
            batch = await asyncio.wait_for(
                consumer.getmany(timeout_ms=1000, max_records=10), timeout=3.0
            )
            for _tp, records in batch.items():
                for rec in records:
                    try:
                        evt = AnalysisCompletedEvent.model_validate_json(rec.value)
                    except Exception:
                        continue
                    if evt.tenant_id == tenant_id and evt.job_id == job_id:
                        return evt
    finally:
        await consumer.stop()
    raise AssertionError(
        f"AnalysisCompletedEvent not found for job {job_id} within {time_limit}s"
    )


async def _wait_for_analysis(
    analysis_repo: MongoAnalysisRepository,
    tenant_id: str,
    job_id: str,
    *,
    time_limit: float = 30.0,
) -> Analysis | None:
    deadline = asyncio.get_event_loop().time() + time_limit
    while asyncio.get_event_loop().time() < deadline:
        analysis = await analysis_repo.get_for_job(tenant_id, job_id)
        if analysis is not None:
            return analysis
        await asyncio.sleep(0.5)
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_analyst_e2e_produces_grounded_analysis(stack: dict) -> None:
    """Full pipeline: publish AnalysisPendingEvent → AnalysisCompletedEvent emitted,
    Analysis persisted to Mongo with grounded insights and citations."""
    settings = stack["settings"]
    tenant_id = stack["tenant_id"]
    job_id = stack["job_id"]
    category_id = stack["category_id"]
    valid_chunk_ids = set(stack["chunk_ids"])

    await _publish_analysis_pending(
        stack["producer"],
        tenant_id=tenant_id, job_id=job_id, category_id=category_id,
    )

    # Wait for the completion event (LLM round-trips dominate; 180s budget)
    event = await _consume_analysis_completed(
        settings, tenant_id=tenant_id, job_id=job_id, time_limit=180.0,
    )

    assert event.success is True
    assert event.analysis_id is not None
    assert event.refinement_loop == 0
    assert 0.0 <= event.score <= 1.0

    # Verify the Analysis landed in Mongo
    analysis = await _wait_for_analysis(stack["analysis_repo"], tenant_id, job_id)
    assert analysis is not None
    assert analysis.id == event.analysis_id
    assert analysis.tenant_id == tenant_id
    assert analysis.category_id == category_id

    # Structural assertions on the LLM output
    assert len(analysis.summary) > 50
    assert len(analysis.insights) >= 1
    assert len(analysis.citations) >= 1

    # Grounding: every supporting_chunk_id must come from the inserted corpus
    for insight in analysis.insights:
        assert insight.supporting_chunk_ids, "Insight has no supporting chunks"
        for cid in insight.supporting_chunk_ids:
            assert cid in valid_chunk_ids, f"Insight cites unknown chunk_id: {cid}"
        assert 0.0 <= insight.confidence <= 1.0

    # Citations point to chunks we inserted
    for cite in analysis.citations:
        assert cite.chunk_id in valid_chunk_ids
        assert cite.excerpt
        assert len(cite.excerpt) <= 500

    # Validator fields are stamped onto the Analysis
    assert analysis.validator_score == event.score
    assert analysis.validator_passed == event.passed
    assert analysis.refinement_loops == 0


async def test_analyst_e2e_carries_refinement_loop(stack: dict) -> None:
    """A refinement-loop=1 event with notes drives the Analyst the same way,
    carries refinement_loop=1 in the completion event and Analysis."""
    settings = stack["settings"]
    tenant_id = stack["tenant_id"]
    job_id = new_id()  # new job_id so idempotency doesn't collide with prior test
    category_id = stack["category_id"]

    await _publish_analysis_pending(
        stack["producer"],
        tenant_id=tenant_id, job_id=job_id, category_id=category_id,
        refinement_loop=1,
        refinement_notes=(
            "Prior analysis was too vague. Emphasize specific named techniques "
            "and the labs that use them."
        ),
    )

    event = await _consume_analysis_completed(
        settings, tenant_id=tenant_id, job_id=job_id, time_limit=180.0,
    )
    assert event.refinement_loop == 1

    if event.success and event.analysis_id:
        analysis = await _wait_for_analysis(stack["analysis_repo"], tenant_id, job_id)
        assert analysis is not None
        assert analysis.refinement_loops == 1
