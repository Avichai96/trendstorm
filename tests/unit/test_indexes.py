"""Unit tests for the index registry.

These tests assert structural properties of the index definitions — not
that they're applied (that needs Mongo). They catch the most common
mistakes:
    - Two indexes with the same name (Mongo would reject; we want fast feedback).
    - TTL on a non-singleton index (Mongo only honors TTL on single-field).
    - Unique index missing tenant_id prefix (multi-tenant safety).
"""
from __future__ import annotations

from collections import Counter

import pytest

from trendstorm.infrastructure.mongo.indexes import INDEXES, IndexSpec, indexes_for_collection
from trendstorm.infrastructure.mongo.schema import Collection


@pytest.mark.unit
class TestIndexRegistry:
    def test_no_duplicate_names_within_collection(self) -> None:
        """Two indexes on the same collection with the same name = bug.

        Mongo would reject the second create; better to fail in CI.
        """
        per_coll: dict[Collection, list[str]] = {}
        for idx in INDEXES:
            per_coll.setdefault(idx.collection, []).append(idx.name)

        for coll, names in per_coll.items():
            counts = Counter(names)
            dups = [name for name, c in counts.items() if c > 1]
            assert not dups, f"Duplicate index names in {coll.value}: {dups}"

    def test_ttl_indexes_are_single_field(self) -> None:
        """Mongo only honors TTL on single-field indexes. A TTL on a
        compound index silently does nothing."""
        for idx in INDEXES:
            if idx.expire_after_seconds is not None:
                assert len(idx.keys) == 1, (
                    f"TTL index {idx.name} has compound keys {idx.keys}; "
                    "Mongo will silently NOT expire any documents."
                )

    def test_tenant_scoped_unique_indexes_include_tenant_id(self) -> None:
        """A unique index must include `tenant_id` as the first key,
        otherwise it enforces uniqueness GLOBALLY across all tenants —
        a multi-tenant bug.

        Exception: collections that are intentionally global, like
        `idempotency` (keyed by an opaque string).
        """
        # Intentionally global collections:
        # - IDEMPOTENCY: keyed by opaque string, no tenant scope
        # - TENANTS: is the root entity; there is no outer tenant_id to scope by
        # - API_KEYS: key_hash_unique must be global — lookup precedes auth
        GLOBAL_OK = {Collection.IDEMPOTENCY, Collection.TENANTS, Collection.API_KEYS}  # noqa: N806  # constant defined inside test method

        for idx in INDEXES:
            if not idx.unique:
                continue
            if idx.collection in GLOBAL_OK:
                continue
            first_field = idx.keys[0][0] if idx.keys else None
            assert first_field == "tenant_id", (
                f"Unique index {idx.name} on {idx.collection.value} "
                f"starts with {first_field!r}, not 'tenant_id'. "
                "This would enforce global uniqueness across tenants."
            )

    def test_every_collection_in_registry_has_at_least_one_index(self) -> None:
        """If a collection has no indexes at all, queries on it are doing
        full collection scans. That's almost certainly a mistake."""
        # Collections we know are managed externally (LangGraph creates its own
        # indexes during setup()). Exclude them from this check.
        EXTERNALLY_MANAGED = {Collection.CHECKPOINTS, Collection.CHECKPOINT_WRITES}  # noqa: N806  # constant defined inside test method

        seen = {idx.collection for idx in INDEXES}
        expected = set(Collection) - EXTERNALLY_MANAGED
        missing = expected - seen
        assert not missing, f"Collections without any indexes: {missing}"


@pytest.mark.unit
class TestIndexSpec:
    def test_to_pymongo_kwargs_minimal(self) -> None:
        spec = IndexSpec(
            collection=Collection.JOBS,
            keys=[("foo", 1)],
            name="test",
        )
        kw = spec.to_pymongo_kwargs()
        assert kw["name"] == "test"
        assert kw["background"] is True
        # Optional flags shouldn't appear when not set.
        assert "unique" not in kw
        assert "expireAfterSeconds" not in kw
        assert "partialFilterExpression" not in kw

    def test_to_pymongo_kwargs_with_ttl(self) -> None:
        spec = IndexSpec(
            collection=Collection.JOBS,
            keys=[("created_at", 1)],
            name="ttl",
            expire_after_seconds=3600,
        )
        kw = spec.to_pymongo_kwargs()
        assert kw["expireAfterSeconds"] == 3600

    def test_to_pymongo_kwargs_with_partial_filter(self) -> None:
        spec = IndexSpec(
            collection=Collection.JOBS,
            keys=[("status", 1)],
            name="partial",
            partial_filter_expression={"status": {"$ne": "completed"}},
        )
        kw = spec.to_pymongo_kwargs()
        assert kw["partialFilterExpression"] == {"status": {"$ne": "completed"}}


@pytest.mark.unit
class TestIndexesByCollection:
    def test_filter_helper(self) -> None:
        jobs_indexes = indexes_for_collection(Collection.JOBS)
        assert len(jobs_indexes) > 0
        for idx in jobs_indexes:
            assert idx.collection == Collection.JOBS
