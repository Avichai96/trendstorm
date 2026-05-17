# ADR 001 — Single-Region Deployment (Multi-Region Deferred)

**Status**: Accepted  
**Date**: 2026-05-25  
**Deciders**: Engineering leads

---

## Context

TrendStorm AI is entering production with Phase 12. The question of deployment topology
(single-region vs. multi-region active-active) must be decided before hardening the
infrastructure.

Multi-region adds significant operational complexity:
- **Kafka**: cross-region replication (MirrorMaker 2 or Confluent Replicator) doubles
  Kafka operational burden and adds replication lag to the ingestion pipeline.
- **MongoDB**: Atlas global clusters or manual replica set across regions introduce
  read/write routing complexity and eventual-consistency trade-offs for a system that
  currently relies on strong-consistency for idempotency checks and job state.
- **ChromaDB**: no built-in cross-region replication. Vectors would need to be replicated
  at the application layer or replaced with a globally-distributed vector store (Pinecone
  serverless, Weaviate Cloud).
- **Stateful LangGraph checkpoints**: MongoDB-backed checkpoints need cross-region
  consistency or a regional fan-out strategy. Neither is straightforward with the current
  `MongoDBSaver`.

At the current stage (Phase 12), we have zero production traffic, one customer, and no
SLA commitment beyond best-effort. Investing 4–6 weeks in multi-region infrastructure
before demonstrating product-market fit is premature.

---

## Decision

**Deploy to a single region (us-east-1) for Phase 12.**

All services run in one Kubernetes cluster. MongoDB is deployed on Atlas (M10 replica set
in us-east-1). Kafka is a managed cluster (Confluent Cloud or MSK) in us-east-1. Redis
is ElastiCache (primary + one replica) in us-east-1.

The Disaster Recovery runbook (`ops/runbooks/disaster-recovery.md`) covers:
- RPO: 1 hour (daily backups, Atlas PITR).
- RTO: 4 hours (infra reprovisioning + restore).

---

## Consequences

### Accepted trade-offs

1. **No geographic redundancy.** An us-east-1 AWS outage takes down TrendStorm entirely.
   Acceptable at this stage — the alternative is 6 weeks of infra work.

2. **Latency for non-US users.** Job processing is async; the latency impact on API
   response times is negligible. SSE streaming is the bottleneck, not API roundtrip.

3. **Kafka consumer rebalance on rolling deploys.** With a single region, rolling deploys
   cause a brief consumer group rebalance. Tolerable with `terminationGracePeriodSeconds=60`
   in Helm worker templates.

### What multi-region would require (deferred to Phase 13+)

1. **Kafka MirrorMaker 2** or Confluent Cloud global clusters for topic replication.
2. **MongoDB Atlas Global Clusters** with zone-based sharding to ensure tenant data
   locality (a tenant's data stays in their geographic zone).
3. **ChromaDB replacement** with a globally-distributed vector store (Pinecone serverless
   has native multi-region support; Weaviate Cloud supports cross-region replication).
4. **Per-region LangGraph checkpointers** with cross-region state sync (or accepting
   that in-flight jobs are lost on regional failover — jobs restart from PENDING).
5. **DNS-based active-active routing** (Route 53 latency routing or Cloudflare).
6. **Cross-region idempotency**: the Redis-backed idempotency store would need to be
   replaced with a strongly-consistent distributed store (e.g. CockroachDB or DynamoDB
   global tables).

### Revisit when

- 10+ paying tenants AND at least one requesting an SLA with 99.9%+ uptime guarantee.
- OR AWS us-east-1 experiences a significant outage affecting TrendStorm during production traffic.
- OR we expand to EMEA/APAC customers who require data residency in their region (triggers
  the tenant data locality requirement regardless of the uptime argument).

---

## Alternatives considered

| Option | Pros | Cons |
|--------|------|------|
| **Active-active multi-region** (rejected) | Highest availability | 6+ weeks engineering, premature |
| **Active-passive (warm standby)** (rejected) | Faster failover than DR | Still requires cross-region data replication setup |
| **Single-region + Velero backups** (this ADR) | Simple, fast to ship | No geographic redundancy |
