# Real-time Streaming

TrendStorm jobs emit Server-Sent Events (SSE) in real time as each pipeline stage completes.

## Basic streaming

```python
async for event in ts.jobs.stream(job_id):
    print(f"[{event.seq}] {event.event_type.value}")
    if event.event_type.is_terminal:
        break
```

The `stream()` call returns immediately with an async iterator. The iterator:

- Replays historical events (from the start or from a resume point)
- Tails live events via SSE
- Closes automatically when a terminal event arrives

## Event types

| Event | Terminal | Description |
|---|---|---|
| `stage_started` | No | A pipeline stage began |
| `stage_completed` | No | A stage finished successfully |
| `stage_failed` | No | A stage failed (job may retry) |
| `progress` | No | General progress update with `pct` in payload |
| `partial_text` | No | Streaming text fragment from the Analyst |
| `citation_added` | No | The Analyst added a citation |
| `review_required` | No | HITL gate: job paused for human review |
| `review_resolved` | No | HITL decision received; job resuming |
| `report_ready` | **Yes** | Report rendered successfully |
| `job_failed` | **Yes** | Unrecoverable pipeline failure |
| `job_rejected` | **Yes** | Analysis rejected by a reviewer |
| `heartbeat` | No | Server keepalive (no business data) |

## Resuming after disconnect

Save the `seq` number and pass it as `last_event_id` on reconnect:

```python
last_seq: int | None = None

async for event in ts.jobs.stream(job_id):
    last_seq = event.seq
    process(event)
    if event.event_type.is_terminal:
        break

# Later, if connection dropped:
async for event in ts.jobs.stream(job_id, last_event_id=last_seq):
    ...
# or equivalently:
async for event in ts.jobs.resume(job_id, last_event_id=last_seq):
    ...
```

## Heartbeat timeout

If the server sends no event (including heartbeats) for 30 seconds, the SDK raises
`HeartbeatTimeout`. Catch it and decide whether to reconnect:

```python
from trendstorm_sdk import HeartbeatTimeout

try:
    async for event in ts.jobs.stream(job_id, heartbeat_timeout=30.0):
        ...
except HeartbeatTimeout:
    print("Stream went silent — reconnecting")
    async for event in ts.jobs.resume(job_id, last_event_id=last_seq):
        ...
```

## Automatic reconnection

The SDK automatically reconnects up to `max_reconnects` times (default 3) on
transient network drops. Set `max_reconnects=0` to disable:

```python
async for event in ts.jobs.stream(job_id, max_reconnects=0):
    ...
```

## HITL events

When a job's analysis is flagged for human review, the stream emits `review_required`
with `payload.review_id`. The job pauses until a reviewer acts. Once resolved,
the stream emits `review_resolved` and the job continues.

```python
async for event in ts.jobs.stream(job_id):
    if event.event_type.value == "review_required":
        review_id = event.payload.get("review_id")
        print(f"Paused for review: {review_id}")
    elif event.event_type.value == "review_resolved":
        decision = event.payload.get("decision")
        print(f"Review resolved: {decision}")
```

See [HITL Reviews](hitl.md) for how to action reviews.
