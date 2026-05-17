# ChromaDB Health

**Alerts**: `ChromaDBUnhealthy` (gauge < 1 for 2 minutes, page)

## Signal
`trendstorm_vector_store_health` gauge drops below 1. This gauge is set by `ChromaVectorStore.health_check()` — called during the API's `/health/ready` endpoint and should also be called periodically by the knowledge worker.

## Impact
- **Knowledge worker**: cannot upsert vectors → `process_document` fails after chunking/Mongo insert; job stalls in `EMBEDDING` after partial work.
- **Analyst worker**: hybrid retrieval's vector path fails; `HybridRetriever` falls back to BM25-only (logs a warning `retrieval_backend_error`). Quality degrades but jobs can still complete.
- **API readiness**: `/health/ready` returns 503 if ChromaDB is down, preventing load balancers from routing traffic (correct behavior).

## Diagnosis

1. **ChromaDB container health**:
   ```bash
   docker ps | grep chromadb
   docker logs trendstorm-chromadb --tail 50
   curl http://localhost:8000/api/v1/heartbeat
   ```
2. **Loki**:
   ```logql
   {service_name="trendstorm-knowledge-worker"} | json |= "chroma" or "vector"
   ```
3. **Disk space**: ChromaDB stores HNSW indexes on disk. A full volume causes writes to fail silently.
   ```bash
   df -h $(docker inspect trendstorm-chromadb --format '{{range .Mounts}}{{.Source}} {{end}}')
   ```
4. **Collection count**: ChromaDB degrades with thousands of collections (one per tenant per model). Check: `curl http://localhost:8000/api/v1/collections | python3 -m json.tool | grep -c '"name"'`

## Remediation

**Container crashed**:
1. `docker restart trendstorm-chromadb`
2. ChromaDB is stateful — restart recovers from the on-disk index. Data is not lost unless the volume was wiped.

**Disk full**:
1. Expand the host volume or docker volume.
2. Prune old collections for inactive tenants: `chromadb.delete_collection("chunks__<tenant_short>__<model_id>")` via a Python one-off script.
3. TTL policy: chunks TTL in Mongo after 1 year, but ChromaDB vectors have no automatic TTL. Orphaned collections from deleted tenants accumulate. Add a nightly cleanup job (Phase 11).

**Connection timeout** (network issue between knowledge-worker and ChromaDB):
1. Both must be on the `trendstorm_trendstorm` Docker network. Check with `docker inspect`.
2. ChromaDB default port is 8000; `VectorSettings.chroma_port` must match.

## Prevention
- Add a scheduled health-check that sets `trendstorm_vector_store_health` on a 30s interval in each worker that uses ChromaDB (not just at startup).
- Add ChromaDB collection count as a gauge metric; alert when it exceeds 500 (approaching performance degradation threshold).
