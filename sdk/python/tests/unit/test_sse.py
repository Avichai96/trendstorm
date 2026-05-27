"""Unit tests for the SSE consumer.

The SSE parser is tested with a mock SSE server using respx/httpx mocks.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from trendstorm_sdk._errors import HeartbeatTimeout, StreamError
from trendstorm_sdk._sse import SSEStream, _SSEFrame, _parse_frames


@pytest.mark.unit
class TestParseFrames:
    def test_single_event(self) -> None:
        lines = ["id: 1", "event: stage_started", "data: {}", ""]
        frames = _parse_frames(lines)
        assert len(frames) == 1
        assert frames[0].id == "1"
        assert frames[0].event == "stage_started"
        assert frames[0].data() == "{}"

    def test_comment_lines_ignored(self) -> None:
        lines = [": heartbeat", "id: 2", "data: {}", ""]
        frames = _parse_frames(lines)
        assert len(frames) == 1
        assert frames[0].id == "2"

    def test_multiline_data(self) -> None:
        lines = ["data: line1", "data: line2", ""]
        frames = _parse_frames(lines)
        assert frames[0].data() == "line1\nline2"

    def test_empty_dispatch_block_skipped(self) -> None:
        lines = ["", "", "id: 1", "data: x", ""]
        frames = _parse_frames(lines)
        assert len(frames) == 1

    def test_no_terminal_newline_still_parsed(self) -> None:
        lines = ["id: 1", "data: hi"]
        frames = _parse_frames(lines)
        assert len(frames) == 1


SAMPLE_EVENT = {
    "event_id": "01ABCDEFGHIJKLMNOPQRSTUVWX",
    "job_id": "01JOBID1234567890123456789",
    "tenant_id": "01TENANT123456789012345678",
    "event_type": "stage_started",
    "seq": 1,
    "stage": "ingesting",
    "payload": {},
    "occurred_at": datetime.now(timezone.utc).isoformat(),
}


def _make_sse_bytes(*events: dict) -> bytes:
    lines = []
    for i, ev in enumerate(events):
        lines.append(f"id: {ev.get('seq', i)}")
        lines.append(f"event: {ev.get('event_type', 'stage_started')}")
        lines.append(f"data: {json.dumps(ev)}")
        lines.append("")
    return "\n".join(lines).encode()


@pytest.mark.unit
class TestSSEStream:
    async def test_yields_typed_event(self) -> None:
        raw = _make_sse_bytes(SAMPLE_EVENT)

        mock_response = MagicMock()
        mock_response.is_success = True

        async def _aiter_lines():
            for line in raw.decode().split("\n"):
                yield line

        mock_response.aiter_lines = _aiter_lines
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.stream.return_value = mock_response

        stream = SSEStream(mock_client, "/v1/jobs/x/stream", {"Authorization": "Bearer t"})
        events = []
        async for ev in stream:
            events.append(ev)

        assert len(events) == 1
        assert events[0].event_type.value == "stage_started"
        assert events[0].seq == 1

    async def test_terminal_event_stops_stream(self) -> None:
        terminal = {**SAMPLE_EVENT, "event_type": "report_ready", "seq": 99}
        raw = _make_sse_bytes(SAMPLE_EVENT, terminal)

        mock_response = MagicMock()
        mock_response.is_success = True

        async def _aiter_lines():
            for line in raw.decode().split("\n"):
                yield line

        mock_response.aiter_lines = _aiter_lines
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.stream.return_value = mock_response

        stream = SSEStream(mock_client, "/v1/jobs/x/stream", {})
        events = []
        async for ev in stream:
            events.append(ev)

        assert len(events) == 2
        assert events[-1].event_type.value == "report_ready"

    async def test_last_event_id_forwarded_on_reconnect(self) -> None:
        error_count = 0
        ok_event = {**SAMPLE_EVENT, "seq": 5}

        async def _iter_first():
            yield "id: 5"
            yield f"data: {json.dumps(ok_event)}"
            yield ""
            raise httpx.ReadTimeout("timeout")

        mock_first = MagicMock()
        mock_first.is_success = True
        mock_first.aiter_lines = _iter_first
        mock_first.__aenter__ = AsyncMock(return_value=mock_first)
        mock_first.__aexit__ = AsyncMock(return_value=None)

        terminal = {**SAMPLE_EVENT, "event_type": "report_ready", "seq": 6}

        async def _iter_second():
            yield f"data: {json.dumps(terminal)}"
            yield ""

        mock_second = MagicMock()
        mock_second.is_success = True
        mock_second.aiter_lines = _iter_second
        mock_second.__aenter__ = AsyncMock(return_value=mock_second)
        mock_second.__aexit__ = AsyncMock(return_value=None)

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.stream.side_effect = [mock_first, mock_second]

        stream = SSEStream(mock_client, "/stream", {}, max_reconnects=2)
        with patch("trendstorm_sdk._sse.asyncio.sleep", new_callable=AsyncMock):
            events = [ev async for ev in stream]

        assert len(events) == 2
        second_call_headers = mock_client.stream.call_args_list[1][1]["headers"]
        assert second_call_headers.get("Last-Event-ID") == "5"

    async def test_non_success_response_raises_stream_error(self) -> None:
        mock_response = MagicMock()
        mock_response.is_success = False
        mock_response.status_code = 404
        mock_response.aread = AsyncMock(return_value=b"not found")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.stream.return_value = mock_response

        stream = SSEStream(mock_client, "/stream", {}, max_reconnects=0)
        with pytest.raises(StreamError):
            async for _ in stream:
                pass
