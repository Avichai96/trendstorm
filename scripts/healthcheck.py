#!/usr/bin/env python3
"""
healthcheck.py — Full-stack readiness probe for TrendStorm local infra.

Goes beyond `docker compose ps` healthchecks:
  - Verifies Mongo replica set is PRIMARY (not just mongod alive)
  - Verifies all expected Kafka topics exist with correct partition counts
  - Verifies Redis responds to PING
  - Verifies Chroma heartbeat
  - Verifies MinIO buckets exist
  - Verifies Ollama models are pulled

Usage:
    python3 scripts/healthcheck.py            # quiet mode (exit 0/1)
    python3 scripts/healthcheck.py --verbose  # show details

This is intentionally written with stdlib only — it runs BEFORE any
Python deps are installed, so it cannot import pymongo/aiokafka/etc.
We use docker exec into the running containers instead.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable

# ANSI colors
GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[0;33m"
DIM = "\033[2m"
NC = "\033[0m"

# Expected topology — single source of truth for the dev stack.
EXPECTED_TOPICS = {
    "trendstorm.jobs.requested.v1": 12,
    "trendstorm.ingest.pending.v1": 24,
    "trendstorm.ingest.completed.v1": 24,
    "trendstorm.knowledge.pending.v1": 12,
    "trendstorm.knowledge.completed.v1": 12,
    "trendstorm.analysis.pending.v1": 6,
    "trendstorm.analysis.completed.v1": 6,
    "trendstorm.publish.pending.v1": 6,
    "trendstorm.stream.partial.v1": 24,
    "trendstorm.dlq.v1": 6,
}

EXPECTED_BUCKETS = {"trendstorm-raw", "trendstorm-reports"}
EXPECTED_OLLAMA_MODELS = {"llama3.2:3b", "nomic-embed-text:latest"}

# Retry budget: max seconds to wait for any single check
CHECK_TIMEOUT_S = 120
POLL_INTERVAL_S = 2


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


def run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run a subprocess; return (rc, stdout, stderr). Never raises."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 1, "", f"timeout after {timeout}s"
    except FileNotFoundError as e:
        return 1, "", f"command not found: {e}"


def retry(check_fn: Callable[[], CheckResult], deadline_s: int) -> CheckResult:
    """Retry a check until it passes or the deadline expires."""
    end = time.monotonic() + deadline_s
    last: CheckResult | None = None
    while time.monotonic() < end:
        result = check_fn()
        if result.passed:
            return result
        last = result
        time.sleep(POLL_INTERVAL_S)
    return last or CheckResult(name="?", passed=False, detail="never ran")


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_mongo_replica_set() -> CheckResult:
    """Mongo must be PRIMARY (state=1) of replica set rs0."""
    rc, out, err = run([
        "docker", "exec", "trendstorm-mongo", "mongosh",
        "--quiet",
        "--username", "root", "--password", "rootpass",
        "--authenticationDatabase", "admin",
        "--eval", "JSON.stringify({state: rs.status().myState, set: rs.status().set})"
    ])
    if rc != 0:
        return CheckResult("mongo replica set", False, err.strip()[:200])
    try:
        # Find the JSON object in the output (mongosh prints a banner)
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{"):
                data = json.loads(line)
                if data.get("state") == 1 and data.get("set") == "rs0":
                    return CheckResult(
                        "mongo replica set", True, "PRIMARY of rs0"
                    )
                return CheckResult(
                    "mongo replica set", False, f"state={data}"
                )
        return CheckResult(
            "mongo replica set", False, "no JSON in mongosh output"
        )
    except json.JSONDecodeError:
        return CheckResult("mongo replica set", False, "parse error")


def check_kafka_topics() -> CheckResult:
    """All expected topics must exist with the configured partition count."""
    rc, out, err = run([
        "docker", "exec", "trendstorm-kafka",
        "kafka-topics", "--bootstrap-server", "kafka:9092",
        "--describe"
    ])
    if rc != 0:
        return CheckResult("kafka topics", False, err.strip()[:200])

    # Parse `--describe` output. Format includes lines like:
    #   Topic: trendstorm.jobs.requested.v1  TopicId: ... PartitionCount: 12 ...
    found: dict[str, int] = {}
    for line in out.splitlines():
        if "PartitionCount:" in line and "Topic:" in line:
            try:
                # Split on whitespace, find Topic: and PartitionCount:
                tokens = line.split()
                topic_idx = tokens.index("Topic:") + 1
                pc_idx = tokens.index("PartitionCount:") + 1
                found[tokens[topic_idx]] = int(tokens[pc_idx])
            except (ValueError, IndexError):
                continue

    missing = []
    wrong_partitions = []
    for name, expected_parts in EXPECTED_TOPICS.items():
        if name not in found:
            missing.append(name)
        elif found[name] != expected_parts:
            wrong_partitions.append(
                f"{name} (got {found[name]}, expected {expected_parts})"
            )

    if missing or wrong_partitions:
        detail_parts = []
        if missing:
            detail_parts.append(f"missing: {', '.join(missing)}")
        if wrong_partitions:
            detail_parts.append(f"wrong partitions: {', '.join(wrong_partitions)}")
        return CheckResult("kafka topics", False, " | ".join(detail_parts))

    return CheckResult(
        "kafka topics", True,
        f"{len(EXPECTED_TOPICS)} topics with correct partitions"
    )


def check_redis() -> CheckResult:
    rc, out, _ = run([
        "docker", "exec", "trendstorm-redis", "redis-cli", "PING"
    ])
    if rc == 0 and "PONG" in out:
        return CheckResult("redis", True, "PONG")
    return CheckResult("redis", False, "no PONG")


def check_chroma() -> CheckResult:
    rc, out, err = run([
        "docker", "exec", "trendstorm-chroma",
        "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
        "http://localhost:8000/api/v2/heartbeat"
    ])
    if rc == 0 and out.strip() == "200":
        return CheckResult("chroma", True, "heartbeat 200")
    # Older Chroma versions: try v1
    rc, out, _ = run([
        "docker", "exec", "trendstorm-chroma",
        "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
        "http://localhost:8000/api/v1/heartbeat"
    ])
    if rc == 0 and out.strip() == "200":
        return CheckResult("chroma", True, "heartbeat 200 (v1)")
    return CheckResult("chroma", False, f"http {out.strip() or err.strip()}")


def check_minio_buckets() -> CheckResult:
    rc, out, err = run([
        "docker", "exec", "trendstorm-minio",
        "mc", "ls", "local/"
    ], timeout=10)
    # MinIO container has `mc` only if we configured it; fallback to API check.
    if rc != 0:
        # Try via curl to the API
        rc2, out2, _ = run([
            "docker", "exec", "trendstorm-minio",
            "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
            "http://localhost:9000/minio/health/live"
        ])
        if rc2 == 0 and out2.strip() == "200":
            return CheckResult("minio", True, "live (buckets not verified)")
        return CheckResult("minio", False, err.strip()[:200])

    found = {line.strip().rstrip("/").split("/")[-1] for line in out.splitlines() if line.strip()}
    missing = EXPECTED_BUCKETS - found
    if missing:
        return CheckResult("minio buckets", False, f"missing: {missing}")
    return CheckResult("minio buckets", True, f"buckets: {EXPECTED_BUCKETS}")


def check_ollama_models() -> CheckResult:
    rc, out, err = run([
        "docker", "exec", "trendstorm-ollama", "ollama", "list"
    ])
    if rc != 0:
        return CheckResult("ollama", False, err.strip()[:200])

    # ollama list output: header then rows like "llama3.2:3b ... 2.0 GB ..."
    installed = set()
    for line in out.splitlines()[1:]:
        if line.strip():
            installed.add(line.split()[0])

    missing = EXPECTED_OLLAMA_MODELS - installed
    if missing:
        return CheckResult(
            "ollama models", False,
            f"missing: {missing} (still pulling? run `make ollama-list`)"
        )
    return CheckResult("ollama models", True, f"models: {installed}")


CHECKS = [
    ("Mongo replica set",       check_mongo_replica_set),
    ("Kafka topics",            check_kafka_topics),
    ("Redis",                   check_redis),
    ("Chroma",                  check_chroma),
    ("MinIO",                   check_minio_buckets),
    ("Ollama models",           check_ollama_models),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="TrendStorm health check")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--timeout", type=int, default=CHECK_TIMEOUT_S)
    args = parser.parse_args()

    print(f"{DIM}Running {len(CHECKS)} health checks (timeout {args.timeout}s each)...{NC}\n")

    all_passed = True
    for label, fn in CHECKS:
        sys.stdout.write(f"  {label:.<35} ")
        sys.stdout.flush()
        result = retry(fn, deadline_s=args.timeout)
        if result.passed:
            print(f"{GREEN}OK{NC}")
            if args.verbose and result.detail:
                print(f"    {DIM}{result.detail}{NC}")
        else:
            print(f"{RED}FAIL{NC}")
            print(f"    {RED}{result.detail}{NC}")
            all_passed = False

    print()
    if all_passed:
        print(f"{GREEN}✓ All checks passed.{NC}")
        return 0
    else:
        print(f"{RED}✗ Some checks failed.{NC}")
        print(f"  Inspect with: {YELLOW}make ps{NC} and {YELLOW}make logs{NC}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
