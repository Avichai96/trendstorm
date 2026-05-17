"""Canonical Mongo collection names.

Every collection used anywhere in the code MUST be defined here, never as
a string literal at the call site. Otherwise typos silently create new
collections on first write (Mongo creates collections lazily) and you
end up with `jobs`, `Jobs`, `job` and zero queries finding all the data.

Reading any of these symbols in code is a hint that someone is talking to
Mongo at that line.
"""
from __future__ import annotations

from enum import StrEnum


class Collection(StrEnum):
    """Canonical Mongo collection names."""

    # ---- Auth (multi-tenant identity) ----
    TENANTS = "tenants"
    API_KEYS = "api_keys"

    # ---- User-curated (long-lived) ----
    CATEGORIES = "categories"
    SOURCES = "sources"

    # ---- Job execution ----
    JOBS = "jobs"
    IDEMPOTENCY = "idempotency"

    # ---- Pipeline outputs ----
    RAW_DOCUMENTS = "raw_documents"
    CHUNKS = "chunks"
    ANALYSES = "analyses"
    REPORTS = "reports"

    # ---- Evaluation pipeline ----
    EVALUATIONS = "evaluations"

    # ---- Billing / cost tracking ----
    COST_LEDGER = "cost_ledger"

    # ---- Outbox pattern ----
    OUTBOX = "outbox"

    # ---- LangGraph-owned (we don't write to these directly) ----
    # These are listed for completeness; the LangGraph saver manages them.
    CHECKPOINTS = "checkpoints"
    CHECKPOINT_WRITES = "checkpoint_writes"
