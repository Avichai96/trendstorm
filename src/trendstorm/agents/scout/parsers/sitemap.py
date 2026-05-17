"""Sitemap XML parser.

Extracts <loc> URLs from sitemap.xml (and sitemap index files, which nest
further sitemap URLs under <sitemap><loc>). The discovered URLs are returned
in ParseResult.discovered_urls so the pipeline can spawn additional fetch
tasks for each URL instead of treating the sitemap text as content.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from xml.etree import ElementTree  # only for ParseError type; parsing uses defusedxml

import defusedxml.ElementTree as SafeET

from trendstorm.shared.errors import ParseError

if TYPE_CHECKING:
    from trendstorm.agents.scout.fetcher import FetchResult
    from trendstorm.agents.scout.parsers import ParseResult


_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def parse_sitemap(result: FetchResult) -> ParseResult:
    """Extract all <loc> URLs from a sitemap or sitemap index document."""
    from trendstorm.agents.scout.parsers import ParseResult

    raw = result.raw_bytes.decode(result.encoding or "utf-8", errors="replace")
    try:
        root = SafeET.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ParseError(
            "Sitemap XML is malformed",
            context={"url": result.url, "source_id": result.source_id, "detail": str(exc)},
        ) from exc

    urls = [loc.text for loc in root.findall(".//sm:loc", _NS) if loc.text]
    if not urls:
        raise ParseError(
            "Sitemap contains no <loc> entries",
            context={"url": result.url, "source_id": result.source_id},
        )

    text = "\n".join(urls)
    return ParseResult(
        text=text,
        discovered_urls=tuple(urls),
        char_count=len(text),
        word_count=len(urls),
    )
