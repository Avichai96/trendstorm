# Phase 2 — Local Infrastructure

**Status**: ✅ Complete

## Summary

`docker-compose.yml` (Mongo+rs0+keyfile / Kafka KRaft / Redis / ChromaDB / MinIO / Ollama with init containers), `docker-compose.obs.yml` (OTel Collector / Jaeger / Prometheus / Loki / Grafana with cross-linked datasources), `docker-compose.dev.yml` (Kafka UI / Mongo Express / Redis Commander), `docker-compose.app.yml` (api + orchestrator-worker). Makefile with `up/up-obs/up-all/up-app`, `seed-indexes`, `check/smoke`, `nuke`. `scripts/healthcheck.py` (stdlib only) verifies all 6 services.

**Kafka topics provisioned**: `trendstorm.jobs.requested.v1`(12), `ingest.{pending,completed}.v1`(24/24), `knowledge.{pending,completed}.v1`(12/12), `analysis.{pending,completed}.v1`(6/6), `publish.pending.v1`(6), `stream.partial.v1`(24), retry topics for ingest/knowledge/analysis at 30s/5m/1h, `dlq.v1`(6). `auto.create.topics=false` — explicit creation only.
