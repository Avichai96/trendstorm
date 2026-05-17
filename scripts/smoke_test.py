#!/usr/bin/env python3
"""
smoke_test.py — Minimal end-to-end verification of the TrendStorm stack.

Performs:
  1. Mongo: insert + read, verify transaction (requires replica set)
  2. Kafka: produce + consume a message
  3. Redis: SET + GET
  4. Chroma: create collection + add + query
  5. MinIO: put + get object
  6. Ollama: tiny generate call

This proves the stack is operational from a Python client's perspective —
the same way the application will use it.

Usage:
    python3 scripts/smoke_test.py

Requires:
    pip install pymongo redis aiokafka chromadb boto3 httpx
    (we'll wrap this in `uv run` in Phase 3 once we have pyproject.toml)
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from contextlib import asynccontextmanager

GREEN = "\033[0;32m"
RED = "\033[0;31m"
NC = "\033[0m"


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{NC} {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}✗{NC} {msg}")


def step(msg: str) -> None:
    print(f"\n{msg}")


# Connection settings (from host's perspective — uses host-mapped ports)
MONGO_URI = os.getenv(
    "SMOKE_MONGO_URI",
    "mongodb://root:rootpass@localhost:27017/?replicaSet=rs0&directConnection=true&authSource=admin",
)
KAFKA_BOOTSTRAP = os.getenv("SMOKE_KAFKA_BOOTSTRAP", "localhost:29092")
REDIS_URL = os.getenv("SMOKE_REDIS_URL", "redis://localhost:6379/0")
CHROMA_HOST = os.getenv("SMOKE_CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.getenv("SMOKE_CHROMA_PORT", "8000"))
MINIO_ENDPOINT = os.getenv("SMOKE_MINIO_ENDPOINT", "http://localhost:9000")
OLLAMA_URL = os.getenv("SMOKE_OLLAMA_URL", "http://localhost:11434")


async def test_mongo() -> bool:
    """Test Mongo with a transaction (requires replica set)."""
    step("[1/6] MongoDB (with transaction)")
    try:
        from pymongo import AsyncMongoClient
    except ImportError:
        try:
            from motor.motor_asyncio import AsyncIOMotorClient as AsyncMongoClient  # type: ignore
        except ImportError:
            fail("pymongo or motor not installed: pip install pymongo")
            return False

    client = AsyncMongoClient(MONGO_URI)
    try:
        db = client.smoke_test
        doc_id = str(uuid.uuid4())

        # Try a transaction — this REQUIRES a replica set.
        async with await client.start_session() as session:
            async with session.start_transaction():
                await db.test.insert_one({"_id": doc_id, "value": 42}, session=session)
                got = await db.test.find_one({"_id": doc_id}, session=session)
                assert got and got["value"] == 42

        # Cleanup
        await db.test.delete_one({"_id": doc_id})
        ok(f"Mongo: insert+read in transaction (replica set OK), doc {doc_id[:8]}…")
        return True
    except Exception as e:  # noqa: BLE001
        fail(f"Mongo: {type(e).__name__}: {e}")
        return False
    finally:
        if hasattr(client, "close"):
            close = client.close()
            if asyncio.iscoroutine(close):
                await close


async def test_kafka() -> bool:
    """Test Kafka produce + consume on a temp topic."""
    step("[2/6] Kafka (produce + consume)")
    try:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
        from aiokafka.admin import AIOKafkaAdminClient, NewTopic
    except ImportError:
        fail("aiokafka not installed: pip install aiokafka")
        return False

    topic = f"smoke_test_{uuid.uuid4().hex[:8]}"
    payload = b'{"hello": "smoke"}'

    admin = AIOKafkaAdminClient(bootstrap_servers=KAFKA_BOOTSTRAP)
    producer = AIOKafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP)
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=KAFKA_BOOTSTRAP,
        auto_offset_reset="earliest",
        group_id=f"smoke_{uuid.uuid4().hex[:8]}",
    )

    try:
        await admin.start()
        try:
            await admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])
        except Exception:  # noqa: BLE001
            pass  # may already exist

        await producer.start()
        await consumer.start()

        await producer.send_and_wait(topic, payload)

        # Consume with a hard deadline
        async def consume_one():
            async for msg in consumer:
                return msg

        msg = await asyncio.wait_for(consume_one(), timeout=10)
        assert msg.value == payload, f"payload mismatch: {msg.value!r}"
        ok(f"Kafka: round-trip via topic {topic}")
        return True
    except Exception as e:  # noqa: BLE001
        fail(f"Kafka: {type(e).__name__}: {e}")
        return False
    finally:
        await consumer.stop()
        await producer.stop()
        try:
            await admin.delete_topics([topic])
        except Exception:  # noqa: BLE001
            pass
        await admin.close()


async def test_redis() -> bool:
    step("[3/6] Redis")
    try:
        from redis.asyncio import from_url
    except ImportError:
        fail("redis not installed: pip install redis")
        return False

    client = from_url(REDIS_URL, decode_responses=True)
    try:
        key = f"smoke:{uuid.uuid4().hex[:8]}"
        await client.set(key, "ok", ex=60)
        got = await client.get(key)
        await client.delete(key)
        assert got == "ok", f"got {got!r}"
        ok("Redis: SET + GET")
        return True
    except Exception as e:  # noqa: BLE001
        fail(f"Redis: {type(e).__name__}: {e}")
        return False
    finally:
        await client.aclose()


async def test_chroma() -> bool:
    step("[4/6] ChromaDB")
    try:
        import chromadb
    except ImportError:
        fail("chromadb not installed: pip install chromadb")
        return False

    try:
        client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
        client.heartbeat()
        coll_name = f"smoke_{uuid.uuid4().hex[:8]}"
        coll = client.get_or_create_collection(coll_name)
        coll.add(
            documents=["hello world", "trendstorm rules"],
            ids=["a", "b"],
            embeddings=[[0.1] * 384, [0.2] * 384],
        )
        result = coll.query(query_embeddings=[[0.1] * 384], n_results=1)
        assert result["ids"][0][0] == "a", f"unexpected: {result}"
        client.delete_collection(coll_name)
        ok("Chroma: collection create + add + query")
        return True
    except Exception as e:  # noqa: BLE001
        fail(f"Chroma: {type(e).__name__}: {e}")
        return False


async def test_minio() -> bool:
    step("[5/6] MinIO (S3 API)")
    try:
        import boto3
        from botocore.client import Config
    except ImportError:
        fail("boto3 not installed: pip install boto3")
        return False

    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=MINIO_ENDPOINT,
            aws_access_key_id="minioadmin",
            aws_secret_access_key="minioadmin",
            config=Config(signature_version="s3v4"),
            region_name="us-east-1",
        )
        key = f"smoke/{uuid.uuid4().hex[:8]}.txt"
        s3.put_object(Bucket="trendstorm-raw", Key=key, Body=b"hello smoke")
        got = s3.get_object(Bucket="trendstorm-raw", Key=key)
        body = got["Body"].read()
        assert body == b"hello smoke", f"body mismatch: {body!r}"
        s3.delete_object(Bucket="trendstorm-raw", Key=key)
        ok(f"MinIO: put + get + delete object {key}")
        return True
    except Exception as e:  # noqa: BLE001
        fail(f"MinIO: {type(e).__name__}: {e}")
        return False


async def test_ollama() -> bool:
    step("[6/6] Ollama (tiny inference)")
    try:
        import httpx
    except ImportError:
        fail("httpx not installed: pip install httpx")
        return False

    async with httpx.AsyncClient(timeout=120) as http:
        try:
            r = await http.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": "llama3.2:3b",
                    "prompt": "Reply with just the word: ok",
                    "stream": False,
                    "options": {"num_predict": 10},
                },
            )
            r.raise_for_status()
            data = r.json()
            response = data.get("response", "").strip()
            ok(f"Ollama: generated {response!r}")
            return True
        except Exception as e:  # noqa: BLE001
            fail(f"Ollama: {type(e).__name__}: {e}")
            return False


async def main() -> int:
    print("TrendStorm smoke test")
    print("=" * 50)
    results = await asyncio.gather(
        test_mongo(),
        test_kafka(),
        test_redis(),
        test_chroma(),
        test_minio(),
        test_ollama(),
        return_exceptions=False,
    )
    print()
    passed = sum(1 for r in results if r is True)
    total = len(results)
    if passed == total:
        print(f"{GREEN}✓ {passed}/{total} smoke tests passed.{NC}")
        return 0
    else:
        print(f"{RED}✗ {passed}/{total} smoke tests passed.{NC}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
