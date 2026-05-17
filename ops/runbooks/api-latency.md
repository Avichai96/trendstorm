# API Latency & Errors

**Alerts**: `APILatencyP99Page` (>2s, page), `APIErrorRateHigh` (>5% errors, page)

## Signal

- **Latency**: p99 of `trendstorm_api_request_duration_seconds` for non-SSE operations exceeds 2s.
- **Errors**: more than 5% of API requests return `status=error` or `status=permanent_error`.

## Impact
Users experience slow or failing API calls. POST /v1/jobs (job creation) blocks; GET /v1/categories returns slowly. The frontend may show loading spinners indefinitely.

## Diagnosis

1. **Grafana → TrendStorm Overview → API panels**: identify which `operation` is slow.
2. **Grafana → Infrastructure → Mongo Pool**: a saturated pool is the most common cause of API latency spikes.
3. **Loki**:
   ```logql
   {service_name="trendstorm-api"} | json | level="error"
   | line_format "{{.event}} {{.error_code}} {{.path}} {{.duration_ms}}"
   ```
4. **Jaeger → trendstorm-api**: check the slowest traces. The OTel auto-instrumentation wraps every FastAPI request; look for slow child spans (Mongo, Redis).
5. Check Redis health:
   ```bash
   docker exec trendstorm-redis redis-cli ping
   docker exec trendstorm-redis redis-cli info stats | grep instantaneous_ops_per_sec
   ```

## Remediation

**Mongo pool saturated** → see [mongo-pool.md](mongo-pool.md)

**Redis unavailable**:
1. `docker restart trendstorm-redis`
2. The API's readiness check (`GET /health/ready`) will fail until Redis reconnects. Kubernetes would drain traffic automatically; in Docker Compose, manually wait.

**Validation errors spiking** (`APIErrorRateHigh` with `error_code: validation_error`):
- A client SDK is sending malformed payloads. Check which `operation` has the errors.
- Check recent API schema changes: a missing required field after a deploy would cause this.

**High 404 rate** (`not_found` errors):
- Usually a client caching stale IDs. Not actionable from the API side; alert may be a false positive. Check if a tenant's category/source was deleted while jobs reference it.

## Prevention
- Add cursor pagination to list endpoints to avoid full-collection scans as data grows.
- Add a circuit breaker on the Mongo client so API fails fast with 503 instead of timing out on every request when Mongo is unhealthy.
