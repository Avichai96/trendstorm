# Publisher Latency

**Alerts**: `PublisherLatencyP99Page` (>60s, page)

## Signal
The p99 of `trendstorm_publisher_render_duration_seconds` exceeds 60s. This covers: MinIO analysis download + Jinja2 markdown render + JSON serialization + Weasyprint PDF render + three MinIO uploads + three Mongo Report inserts.

## Impact
Jobs stay in `PUBLISHING` stage. SSE clients receive `STAGE_STARTED` (publishing) but no `REPORT_READY`. The orchestrator's `_handle_publish_completed` is never called, so the job never reaches `COMPLETED`.

## Diagnosis

1. **Grafana → Publisher → Render Duration p99 by Format**: determine if all formats are slow or just PDF.
2. **Jaeger → trendstorm-publisher-worker → publisher.render**: look for the slowest child spans.
3. **Loki**:
   ```logql
   {service_name="trendstorm-publisher-worker"} | json | level="warning"
   ```
   Look for `pdf_render_failed` (Weasyprint) or slow MinIO upload logs.
4. Check MinIO health: `curl http://localhost:9000/minio/health/live`

## Remediation

**Weasyprint PDF slow or failing**:
- PDF rendering is best-effort (`PublisherService.publish` catches and logs PDF failures). Check whether `pdf_report_id` is `None` in recent `PublishCompletedEvent` messages.
- On Linux Docker: missing Pango/Cairo libs cause Weasyprint to fail silently. Verify the publisher Docker image includes: `fonts-dejavu libpango-1.0-0 libpangoft2-1.0-0 libcairo2`.
- If PDF is timing out: add a `asyncio.wait_for` timeout around `render_pdf()` and log it; PDF failure should not block MD/JSON.

**MinIO slow**:
- Check MinIO container health: `docker logs trendstorm-minio --tail 50`
- Check disk space on the host: MinIO stops accepting writes when the data volume is full.
- Check network: MinIO and publisher-worker must be on the same Docker network (`trendstorm_trendstorm`).

**Slow Mongo Report inserts**:
- Three sequential inserts per publish job. Check `trendstorm_mongo_pool_utilization_ratio` for `publisher-worker`. See [mongo-pool.md](mongo-pool.md).

## Prevention
- Add per-render format timeouts: PDF 30s, MD/JSON 5s each.
- Add `publisher_bytes_uploaded_total` to catch unexpectedly large reports that slow uploads.
