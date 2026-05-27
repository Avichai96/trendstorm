# Error Handling

All SDK errors are subclasses of `TrendStormError`, enabling a single catch-all:

```python
from trendstorm_sdk import TrendStormError

try:
    ...
except TrendStormError as e:
    print(f"TrendStorm error: {e}")
```

## Error hierarchy

```
TrendStormError
├── ConfigurationError     bad constructor args (missing key, invalid URL)
├── StreamError            SSE stream parse or unrecoverable connection error
├── HeartbeatTimeout       no event received within heartbeat window
└── APIError               server returned an HTTP error
    ├── RateLimited        429 — check retry_after attribute
    ├── NotFound           404
    ├── Unauthorized       401 / 403
    ├── ValidationError    422 — request body failed server validation
    └── ServerError        5xx — TrendStorm server error
```

## Common patterns

### Catching specific errors

```python
from trendstorm_sdk import NotFound, RateLimited, ServerError

try:
    job = await ts.jobs.get("nonexistent-id")
except NotFound:
    print("Job not found")
except RateLimited as e:
    print(f"Rate limited. Retry in {e.retry_after}s")
except ServerError as e:
    print(f"Server error {e.status_code}: {e.error_code}")
```

### Inspecting API errors

Every `APIError` carries:

```python
try:
    ...
except APIError as e:
    print(e.status_code)      # 422
    print(e.error_code)       # "validation_error"
    print(e.message)          # "source_ids must not be empty"
    print(e.request_id)       # "req_01ABC..." (for support tickets)
    print(e.correlation_id)   # trace correlation ID
    print(e.raw)              # full JSON body dict
```

### Rate limit handling

The SDK automatically retries 429 responses (up to `max_retries`, default 5),
honouring the `Retry-After` header. If you exhaust all retries, `RateLimited` is raised:

```python
from trendstorm_sdk import RateLimited, TrendStormClient

async with TrendStormClient(api_key="...", max_retries=3) as ts:
    try:
        await ts.jobs.create(...)
    except RateLimited as e:
        print(f"Still rate limited after retries. Wait {e.retry_after}s")
```

### Heartbeat timeout

```python
from trendstorm_sdk import HeartbeatTimeout

try:
    async for event in ts.jobs.stream(job_id, heartbeat_timeout=30.0):
        ...
except HeartbeatTimeout:
    print("Stream silent for 30s — network issue?")
    # Reconnect manually:
    async for event in ts.jobs.resume(job_id, last_event_id=last_seq):
        ...
```

## Retries

The SDK retries on:
- `429 Too Many Requests` — exponential backoff, honouring `Retry-After`
- `500 / 502 / 503 / 504` — exponential backoff (1s, 2s, 4s … max 60s)
- `httpx.ConnectError` / `httpx.ReadTimeout` — exponential backoff

Non-retryable errors raise immediately:
- `400 Bad Request`
- `401 Unauthorized` / `403 Forbidden`
- `404 Not Found`
- `422 Validation Error`

Configure retry count:
```python
TrendStormClient(api_key="...", max_retries=0)  # disable retries
TrendStormClient(api_key="...", max_retries=10) # more aggressive
```
