"""JSON API response parser.

Stores the pretty-printed JSON as text. Source-specific field mappings
(extracting "title", "body", etc. from a known API schema) belong in a
separate layer; this parser is intentionally generic.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from trendstorm.shared.errors import ParseError

if TYPE_CHECKING:
    from trendstorm.agents.scout.fetcher import FetchResult
    from trendstorm.agents.scout.parsers import ParseResult


def parse_api(result: FetchResult) -> ParseResult:
    """Decode a JSON response and store it as indented text."""
    from trendstorm.agents.scout.parsers import ParseResult

    raw = result.raw_bytes.decode(result.encoding or "utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ParseError(
            "API response is not valid JSON",
            context={"url": result.url, "source_id": result.source_id, "detail": str(exc)},
        ) from exc

    text = json.dumps(data, ensure_ascii=False, indent=2)
    return ParseResult(
        text=text,
        char_count=len(text),
        word_count=len(text.split()),
    )
