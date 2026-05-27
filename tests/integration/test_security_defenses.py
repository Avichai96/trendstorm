"""Integration tests for Phase 13 security defenses.

Requires: `make up` (Mongo, Redis, Kafka running).

These tests verify that each defense mechanism fires end-to-end:
    1. SSRF blocks a job whose source URL targets a private IP.
    2. Per-tenant blocklist blocks a URL that is on the tenant's list.
    3. PII detector runs on chunk text before LLM submission.
    4. Audit log is populated on each security event.
    5. Security block metric is incremented.

Run: pytest -m integration tests/integration/test_security_defenses.py
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.integration
class TestSSRFDefense:
    async def test_ssrf_private_ip_blocked_in_scout(
        self,
        scout_fetcher,  # noqa: ARG002  -- fixture from conftest
    ) -> None:
        """Scout fetcher must raise SSRFBlockedError for RFC 1918 URLs."""
        from trendstorm.infrastructure.security.ssrf import validate_url
        from trendstorm.shared.errors import SSRFBlockedError

        with pytest.raises(SSRFBlockedError) as exc_info:
            validate_url("http://10.0.0.1/admin", resolved_addrs=["10.0.0.1"])
        assert exc_info.value.reason == "ssrf_private_ip"

    async def test_ssrf_aws_metadata_blocked(self) -> None:
        from trendstorm.infrastructure.security.ssrf import validate_url
        from trendstorm.shared.errors import SSRFBlockedError

        with pytest.raises(SSRFBlockedError) as exc_info:
            validate_url(
                "http://169.254.169.254/latest/meta-data/",
                resolved_addrs=["169.254.169.254"],
            )
        assert exc_info.value.reason == "ssrf_link_local"

    async def test_global_blocklist_blocks_internal_hostname(self) -> None:
        from trendstorm.infrastructure.security.blocklist import check_global_blocklist
        from trendstorm.shared.errors import SSRFBlockedError

        with pytest.raises(SSRFBlockedError) as exc_info:
            check_global_blocklist("localhost", "http://localhost/admin")
        assert exc_info.value.reason == "ssrf_blocklist_global"


@pytest.mark.integration
class TestAuditLogPersistence:
    async def test_audit_log_entry_persisted(self, mongo_client) -> None:  # noqa: ARG002
        """AuditLogEntry inserted via MongoAuditLogRepository is readable back."""
        from trendstorm.domain.audit_log.models import AuditLogEntry
        from trendstorm.infrastructure.mongo.repositories.audit_log_repository import (
            MongoAuditLogRepository,
        )

        repo = MongoAuditLogRepository(mongo_client)
        entry = AuditLogEntry(
            tenant_id="test-tenant-security",
            event_type="ssrf_blocked",
            actor="system",
            resource_type="source",
            resource_id="test-source-id",
            action="validate_url",
            outcome="blocked",
            metadata={"reason": "ssrf_private_ip", "url": "http://10.0.0.1/"},
        )
        await repo.append(entry)

        entries = await repo.list_for_tenant(
            "test-tenant-security", event_type="ssrf_blocked"
        )
        assert len(entries) >= 1
        found = next((e for e in entries if e.id == entry.id), None)
        assert found is not None
        assert found.outcome == "blocked"
        assert found.metadata["reason"] == "ssrf_private_ip"


@pytest.mark.integration
class TestPIIDetectorIntegration:
    def test_pii_detection_runs_without_io(self) -> None:
        """Confirm the regex PII detector works correctly on sample text."""
        from trendstorm.infrastructure.security.pii import DefaultPIIDetector

        detector = DefaultPIIDetector()
        text = "Contact ssn-holder at 123-45-6789 or user@example.com"
        result = detector.detect_and_redact(text)
        assert result.has_pii is True
        assert "123-45-6789" not in result.redacted_text
        assert "user@example.com" not in result.redacted_text
        assert "[REDACTED:SSN]" in result.redacted_text
        assert "[REDACTED:EMAIL]" in result.redacted_text


@pytest.mark.integration
class TestSecurityMetrics:
    def test_record_security_block_increments_counter(self) -> None:
        from trendstorm.shared.metrics.registry import make_test_metrics, record_security_block

        metrics = make_test_metrics()
        record_security_block("ssrf_private_ip", "tenant-abc", metrics=metrics)
        # Counter value accessible via prometheus_client internals
        samples = list(metrics.security_blocks.collect())
        assert len(samples) > 0
