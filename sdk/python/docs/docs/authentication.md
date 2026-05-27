# Authentication

TrendStorm supports two authentication methods: **API keys** and **OAuth 2.0**.

## API keys (recommended)

API keys are the simplest way to authenticate. Provision one from the TrendStorm dashboard or via the API.

```python
from trendstorm_sdk import TrendStormClient

# Explicit key
async with TrendStormClient(api_key="ts_live_...") as ts:
    ...

# From environment variable (recommended for production)
# export TRENDSTORM_API_KEY="ts_live_..."
async with TrendStormClient() as ts:
    ...
```

### Key format

- `ts_live_` prefix — production tenant
- `ts_test_` prefix — sandbox tenant (rate limits apply, no billing)

### Creating keys via SDK

```python
created = await ts.api_keys.create(name="my-worker")
secret = created.key   # ← store this immediately; shown ONCE
print(f"Key: {created.key_prefix}...")

# List keys (plaintext never returned again)
keys = await ts.api_keys.list()

# Revoke
await ts.api_keys.revoke(key_id)
```

### Reviewer keys

To resolve HITL reviews, an API key needs the `reviewer` role.
Create reviewer keys via the server admin API (not the SDK); the role is
set server-side and cannot be self-assigned.

## OAuth 2.0

For applications that need fine-grained user-level access:

```python
async with TrendStormClient(
    oauth_token="eyJhbGc...",
    oauth_refresh_token="dGhpcyBpcyBh...",
    oauth_token_url="https://auth.trendstorm.io/oauth/token",
) as ts:
    ...
```

The SDK auto-refreshes the access token when it expires (detected by checking
`expires_at` before each request). Refresh is concurrency-safe: if two coroutines
both detect expiry simultaneously, only one refresh request is issued.

### OAuth environment variables

```bash
export TRENDSTORM_OAUTH_TOKEN="eyJhbGc..."
```

## Base URL

Override the base URL for local development or staging:

```bash
export TRENDSTORM_BASE_URL="http://localhost:8080"
```

Or:

```python
TrendStormClient(api_key="ts_test_...", base_url="http://localhost:8080")
```
