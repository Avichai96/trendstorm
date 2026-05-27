"""Server-Sent Events consumer for the TrendStorm SDK.

Implements a custom SSE parser over httpx streaming because the available SSE
libraries don't cleanly handle:
  - ``Last-Event-ID`` header forwarding on reconnect
  - Typed event parsing (our events have both ``event:`` and ``data:`` lines)
  - Heartbeat timeout detection
  - Automatic reconnect capped at N attempts

Usage::

    async for event in SSEStream(client, url, last_event_id=None):
        print(event.event_type, event.payload)
        if event.event_type.is_terminal:
            break  # stream closes automatically too

The generator closes the underlying httpx stream on:
  - A terminal event (REPORT_READY, JOB_FAILED, JOB_REJECTED)
  - Max reconnects exhausted
  - ``HeartbeatTimeout`` (no event for ``heartbeat_timeout`` seconds)
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

from trendstorm_shared.models import StreamEvent
from trendstorm_shared.types import StreamEventType

from ._errors import HeartbeatTimeout, StreamError

logger = logging.getLogger(__name__)

_DEFAULT_HEARTBEAT_TIMEOUT = 30.0
_DEFAULT_MAX_RECONNECTS = 3
_RECONNECT_DELAY = 1.0


@dataclass
class _SSEFrame:
    """Raw parsed SSE frame before JSON decoding."""
    id: str | None = None
    event: str | None = None
    data_lines: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.data_lines and self.event is None

    def data(self) -> str:
        return "\n".join(self.data_lines)


def _parse_frames(lines: list[str]) -> list[_SSEFrame]:
    """Parse a block of SSE text lines into frames (dispatch blocks)."""
    frames: list[_SSEFrame] = []
    current = _SSEFrame()
    for line in lines:
        if line.startswith(":"):
            continue  # heartbeat comment
        if not line:
            if not current.is_empty():
                frames.append(current)
            current = _SSEFrame()
        elif ":" in line:
            key, _, value = line.partition(":")
            value = value.lstrip(" ")
            if key == "id":
                current.id = value
            elif key == "event":
                current.event = value
            elif key == "data":
                current.data_lines.append(value)
    if not current.is_empty():
        frames.append(current)
    return frames


class SSEStream:
    """Async iterator that yields typed ``StreamEvent`` objects from an SSE endpoint.

    Args:
        client:              Shared ``httpx.AsyncClient`` from ``TrendStormClient``.
        url:                 Full URL of the SSE stream endpoint.
        auth_headers:        Dict of auth headers to attach on each connection.
        last_event_id:       Resume from this seq number (sent as ``Last-Event-ID``).
        heartbeat_timeout:   Seconds to wait before raising ``HeartbeatTimeout``.
        max_reconnects:      Max automatic reconnects on connection drop.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        url: str,
        auth_headers: dict[str, str],
        *,
        last_event_id: int | None = None,
        heartbeat_timeout: float = _DEFAULT_HEARTBEAT_TIMEOUT,
        max_reconnects: int = _DEFAULT_MAX_RECONNECTS,
    ) -> None:
        self._client = client
        self._url = url
        self._auth_headers = auth_headers
        self._last_event_id = last_event_id
        self._heartbeat_timeout = heartbeat_timeout
        self._max_reconnects = max_reconnects

    def __aiter__(self) -> AsyncIterator[StreamEvent]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[StreamEvent]:
        reconnects = 0
        while reconnects <= self._max_reconnects:
            try:
                async for event in self._connect():
                    yield event
                    if event.event_type.is_terminal:
                        return
                return  # clean EOF from server
            except (httpx.StreamError, httpx.ConnectError, httpx.ReadTimeout) as exc:
                reconnects += 1
                if reconnects > self._max_reconnects:
                    raise StreamError(f"SSE stream failed after {self._max_reconnects} reconnects: {exc}") from exc
                logger.warning(
                    "SSE connection dropped (%s); reconnecting in %.1fs (attempt %d/%d)",
                    exc, _RECONNECT_DELAY, reconnects, self._max_reconnects,
                )
                await asyncio.sleep(_RECONNECT_DELAY)

    async def _connect(self) -> AsyncIterator[StreamEvent]:
        headers = {
            **self._auth_headers,
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }
        if self._last_event_id is not None:
            headers["Last-Event-ID"] = str(self._last_event_id)

        async with self._client.stream("GET", self._url, headers=headers) as response:
            if not response.is_success:
                body = await response.aread()
                raise StreamError(f"SSE endpoint returned HTTP {response.status_code}: {body[:200]}")

            buffer: list[str] = []
            async for line in self._aiter_lines_with_timeout(response):
                buffer.append(line)
                if line == "":
                    # Empty line = end of dispatch block
                    for frame in _parse_frames(buffer):
                        buffer.clear()
                        event = self._decode_frame(frame)
                        if event is not None:
                            yield event
                    buffer.clear()

    async def _aiter_lines_with_timeout(
        self, response: httpx.Response
    ) -> AsyncIterator[str]:
        """Yield lines with per-line heartbeat timeout enforcement."""
        async def _inner() -> AsyncIterator[str]:
            async for line in response.aiter_lines():
                yield line

        iterator = _inner().__aiter__()
        while True:
            try:
                line = await asyncio.wait_for(
                    iterator.__anext__(),
                    timeout=self._heartbeat_timeout,
                )
            except StopAsyncIteration:
                return
            except TimeoutError:
                raise HeartbeatTimeout(self._heartbeat_timeout)
            yield line

    def _decode_frame(self, frame: _SSEFrame) -> StreamEvent | None:
        import json

        if not frame.data_lines:
            return None

        raw_data = frame.data()

        # Update last seen event ID for reconnect.
        if frame.id is not None:
            try:
                self._last_event_id = int(frame.id)
            except ValueError:
                pass

        try:
            payload = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            logger.warning("SSE data is not valid JSON: %s — %s", exc, raw_data[:200])
            return None

        try:
            return StreamEvent.model_validate(payload)
        except Exception as exc:
            logger.warning("SSE payload failed StreamEvent validation: %s", exc)
            return None
