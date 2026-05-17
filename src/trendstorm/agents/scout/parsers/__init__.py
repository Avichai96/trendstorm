"""Content-type router for Scout parsers.

Each parser receives a FetchResult and returns a ParseResult. The route()
function dispatches by content_type. For generic XML (application/xml,
text/xml), it peeks at the root element tag to decide between sitemap and
RSS before falling back to the HTML extractor.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from xml.etree import ElementTree  # only for ParseError type; parsing uses defusedxml

import defusedxml.ElementTree as SafeET

from trendstorm.shared.errors import ParseError
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.agents.scout.fetcher import FetchResult

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Extracted content from a fetched resource."""

    text: str
    title: str | None = None
    language: str | None = None
    char_count: int = 0
    word_count: int = 0
    # Non-empty only for sitemaps; pipeline uses these to schedule more fetches.
    discovered_urls: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Content-type → parser dispatch table
# ---------------------------------------------------------------------------

def route(result: FetchResult) -> ParseResult:
    """Dispatch a FetchResult to the correct parser by content-type."""
    from trendstorm.agents.scout.parsers.api import parse_api
    from trendstorm.agents.scout.parsers.html import parse_html
    from trendstorm.agents.scout.parsers.rss import parse_rss

    ct = result.content_type

    if ct in {"text/html", "application/xhtml+xml", "text/plain"}:
        return parse_html(result)
    if ct in {"application/rss+xml", "application/atom+xml"}:
        return parse_rss(result)
    if ct == "application/json":
        return parse_api(result)
    if ct in {"application/xml", "text/xml"}:
        return _route_xml(result)

    raise ParseError(
        f"No parser registered for content-type {ct!r}",
        context={"content_type": ct, "url": result.url, "source_id": result.source_id},
    )


def _route_xml(result: FetchResult) -> ParseResult:
    """Peek at the XML root element to pick between sitemap, RSS, and HTML."""
    from trendstorm.agents.scout.parsers.html import parse_html
    from trendstorm.agents.scout.parsers.rss import parse_rss
    from trendstorm.agents.scout.parsers.sitemap import parse_sitemap

    try:
        root = SafeET.fromstring(
            result.raw_bytes.decode(result.encoding or "utf-8", errors="replace")
        )
    except ElementTree.ParseError:
        return parse_html(result)

    # Strip namespace prefix: {http://...}urlset → urlset
    local = root.tag.split("}")[-1].lower() if "}" in root.tag else root.tag.lower()

    if local in {"urlset", "sitemapindex"}:
        return parse_sitemap(result)
    if local in {"rss", "feed", "channel"}:
        return parse_rss(result)

    logger.debug("xml_no_match_fallback", tag=local, url=result.url)
    return parse_html(result)
