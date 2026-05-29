"""RSS/Atom feed parser using feedparser.

Each feed entry becomes a titled section in the output text. Entry bodies
often contain inline HTML (especially Atom `<content>`); we strip that with
BeautifulSoup so the text store receives clean prose, not markup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import feedparser
from bs4 import BeautifulSoup

from trendstorm.shared.errors import ParseError
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.agents.scout.fetcher import FetchResult
    from trendstorm.agents.scout.parsers import ParseResult

logger = get_logger(__name__)


def _strip_html(fragment: str) -> str:
    """Strip HTML tags from a feed entry body."""
    return BeautifulSoup(fragment, "lxml").get_text(separator=" ", strip=True)


def parse_rss(result: FetchResult) -> ParseResult:
    """Extract entries from an RSS/Atom feed."""
    from trendstorm.agents.scout.parsers import ParseResult

    raw = result.raw_bytes.decode(result.encoding or "utf-8", errors="replace")
    feed = feedparser.parse(raw)

    # feedparser sets bozo=True for malformed XML but still attempts a parse.
    # Treat as failure only when both bozo=True AND no entries were found.
    if feed.bozo and not feed.entries:
        exc_msg = str(getattr(feed, "bozo_exception", "unknown"))
        raise ParseError(
            "Feed is malformed and contains no entries",
            context={"url": result.url, "source_id": result.source_id, "detail": exc_msg},
        )

    parts: list[str] = []
    for entry in feed.entries:
        entry_title: str = entry.get("title", "").strip()
        # Prefer full content over summary when available.
        content_list: list[dict[str, str]] = entry.get("content", [])
        body_html: str = (
            content_list[0].get("value", "") if content_list else entry.get("summary", "")
        )
        body = _strip_html(body_html) if body_html else ""

        if entry_title:
            parts.append(f"## {entry_title}")
        if body:
            parts.append(body)

    text = "\n\n".join(parts)
    feed_title: str | None = feed.feed.get("title") if hasattr(feed, "feed") else None

    return ParseResult(
        text=text,
        title=feed_title,
        char_count=len(text),
        word_count=len(text.split()),
    )
