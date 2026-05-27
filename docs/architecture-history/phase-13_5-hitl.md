# Phase 13.5 — Human-in-the-Loop (HITL) Review Queue

## What was built

A post-analysis human review gate that allows operators or tenants to approve, reject, or request refinement of analyses before they are published to end users. Flagged analyses pause the LangGraph pipeline; a reviewer acts via REST API; the orchestrator resumes based on the decision. An auto-reject sweeper handles SLA timeout.

## New deployable service

**`trendstorm-review-timeout-worker`** — a polling loop (not Kafka consumer) that scans the `reviews` collection every 60 seconds for `status=pending` entries whose `timeout_at < now()`, marks each as `timed_out`, and publishes `ReviewResolvedEvent(decision="reject")` to `trendstorm.review.resolved.v1`. Deploy as `strategy: Recreate` (single replica). See `orchestration/workers/review_timeout_worker.py`.

## Domain additions

### `domain/reviews/models.py`
`ReviewRequest` — per-analysis review record. Key fields: `status` (PENDING/APPROVED/REJECTED/REFINEMENT_REQUESTED/TIMED_OUT), `timeout_at` (absolute UTC), `sla_seconds`, `reviewer_id` (nullable until resolved). `ReviewDecision` enum: `approve | reject | request_refinement`.

### `domain/tenant_settings/models.py`
`TenantSettings` — per-tenant HITL configuration: `hitl_mode` (OFF/ALWAYS/FLAGGED_ONLY), `hitl_validator_threshold` (default 0.7), `hitl_cost_threshold_usd` (optional), `hitl_timeout_hours` (default 48). `DEFAULT_TENANT_SETTINGS` has `hitl_mode=OFF` — absent rows in Mongo behave identically to existing tenants. No behavior change unless a tenant row is explicitly created.

## New stages and status codes

`Stage.AWAITING_REVIEW` and `Stage.REJECTED` added to the stage machine (`agents/stages.py`). `REJECTED` is terminal (distinct from `FAILED` — clearer user signal: the analysis was reviewed and declined, not broken). Transition rules: `ANALYZING → AWAITING_REVIEW`, `AWAITING_REVIEW → ANALYZING | PUBLISHING | REJECTED | CANCELLED`. `AWAITING_REVIEW → FAILED` is intentionally absent; the review path always terminates via `REJECTED`, never via `FAILED`.

`JobStatus.AWAITING_REVIEW` and `JobStatus.REJECTED` added to `shared/types/__init__.py`.

`schema_version` bumped to 2 on `JobState`. Three new fields: `pending_review_id: str | None`, `review_decision_comment: str | None`, `skip_hitl_gate: bool = False`.

## `skip_hitl_gate` — the re-gating guard

When a reviewer chooses `request_refinement`, the orchestrator publishes a new `AnalysisPendingEvent`. When that analysis completes, `after_analyze` routes again to `review_gate_node`. Without a guard, the node would gate the same analysis a second time. The orchestrator sets `skip_hitl_gate=True` when injecting state on any review resolution (approve, reject, or refinement). `review_gate_node` checks this flag first and returns `{stage: PUBLISHING, skip_hitl_gate: False}` immediately, resetting the flag for future analysis cycles.

## LangGraph integration

`review_gate_node` (`agents/orchestrator/nodes.py`) is dual-mode per the codebase pattern. With no Kafka producer in config (unit tests), it always returns `{stage: PUBLISHING}`. In production: loads `TenantSettings`, evaluates the flagging criteria, either passes through or creates a `ReviewRequest` + publishes `ReviewRequestedEvent`.

`after_analyze` now routes to `NODE_REVIEW_GATE` instead of `NODE_PUBLISH`. `after_review_gate` maps state stage → node (`PUBLISHING → NODE_PUBLISH`, `AWAITING_REVIEW → NODE_END`, else `NODE_FAIL`).

Graph resume: on `approve` the orchestrator calls `aupdate_state(config, {stage: PUBLISHING, skip_hitl_gate: True}, as_node=NODE_REVIEW_GATE)` then `astream(None, interrupt_after=[NODE_PUBLISH])`. On `request_refinement`: injects `{stage: ANALYZING, skip_hitl_gate: True, review_decision_comment: comment}` then publishes `AnalysisPendingEvent` directly (does not call `astream`). On `reject`: updates job status to REJECTED, emits `JOB_REJECTED` SSE event.

## Auth: roles system

`roles: list[str]` added to `ApiKey` and `AuthContext` (backward-compatible, defaults to `[]`). `create_key` accepts optional `roles` parameter. `authenticate_by_key` and `authenticate_by_jwt` both propagate roles. `require_role(role)` FastAPI dependency in `utils/headers_docs.py` — returns a `Depends(_check)` that reads `request.state.auth_context.roles` and raises HTTP 403 if the role is absent. The `/v1/reviews` router requires `reviewer` role.

## API: `/v1/reviews`

Three endpoints on `api/routers/reviews.py`: `GET /v1/reviews` (paginated list with status filter), `GET /v1/reviews/{id}` (single review detail), `POST /v1/reviews/{id}/resolve` (resolve with atomic Mongo transaction: resolve + outbox entry). Resolver creates an `OutboxEntry` so the orchestrator learns about the decision via Kafka (same pattern as job creation). Both the ReviewRequest update and the OutboxEntry are written in the same `start_transaction()` session — atomicity identical to `JobService.create_job`.

## Kafka: two new topics

`trendstorm.review.requested.v1` (6 partitions, 7-day retention) — `ReviewRequestedEvent` published by `review_gate_node`. `trendstorm.review.resolved.v1` (6 partitions, 7-day retention) — `ReviewResolvedEvent` published by the outbox relay (from `/v1/reviews/{id}/resolve`) and by the timeout sweeper. Both added to `kafka-init` in `docker-compose.yml`.

## SSE events

`StreamEventType.JOB_REJECTED` (terminal), `REVIEW_REQUIRED`, `REVIEW_RESOLVED` added to `domain/streaming/events.py`. `JOB_REJECTED.is_terminal = True`. Orchestrator emits `REVIEW_REQUIRED` when a job enters `AWAITING_REVIEW`, `REVIEW_RESOLVED` on approve, `JOB_REJECTED` on reject.

## Observability

Three new Prometheus metrics in `shared/metrics/registry.py`:
- `trendstorm_reviews_pending` (Gauge, label: `tenant_id_hash`) — current pending count.
- `trendstorm_reviews_pending_oldest_created_at` (Gauge, label: `tenant_id_hash`) — Unix timestamp of oldest pending review; used by `PendingReviewsAgingHigh` alert PromQL: `(time() - min(gauge)) / 3600 > 38.4`.
- `trendstorm_review_resolution_seconds` (Histogram, label: `decision`) — reviewer decision latency.
- `trendstorm_review_timeout_total` (Counter) — sweeper auto-reject count.

Two Prometheus alert groups in `docker/config/prometheus-alerts.yml`: `PendingReviewsAgingHigh` (page at 80% of SLA), `PendingReviewsAgingWarning` (warn at 24h), `ReviewTimeoutSweepSpike` (warn when sweeper rate > 2/min). Runbook: `ops/runbooks/review-aging.md`. Two SLO entries in `ops/slo.yml`: `hitl-review-aging` and `hitl-review-resolution-latency`.

## Non-obvious decisions

**`list_expired_pending` is the only cross-tenant query.** The sweeper needs to process ALL expired reviews regardless of tenant. This method intentionally bypasses `_tenant_query()` — it is the documented exception, scoped to the sweeper only. All other `MongoReviewRepository` methods funnel through `_tenant_query()`.

**Review resolve goes through the outbox.** `/v1/reviews/{id}/resolve` does NOT call Kafka directly. It writes the resolve + OutboxEntry in a Mongo transaction. If Kafka is down during a resolve, the decision is still durably recorded and drains on recovery. Direct Kafka publish from an API handler would leave the review resolved but the orchestrator never notified.

**`AWAITING_REVIEW → FAILED` is not a valid transition.** Failing a job mid-review is a different operator decision from rejecting the analysis. If a system failure occurs during review, the sweeper auto-rejects on timeout — there is no mechanism to `FAIL` from `AWAITING_REVIEW` by design.

**`schema_version=2` is metadata only.** There is no migration path in the codebase because LangGraph checkpoints store the full state dict — old `schema_version=1` jobs simply miss the three HITL fields, and Pydantic fills them with defaults on deserialization. If a v1 job checkpoint is replayed, it behaves as `skip_hitl_gate=False`, `pending_review_id=None` — which is correct.
