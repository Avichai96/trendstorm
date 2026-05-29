"""HTML content extractor using trafilatura with a BeautifulSoup fallback.

trafilatura is the primary extractor — it's tuned for news/article pages and
handles boilerplate removal (nav, ads, footers) well. The BS4 fallback fires
when trafilatura returns None, which typically means JS-heavy SPAs, login
walls, or error pages where there is no extractable main content block.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import trafilatura
from bs4 import BeautifulSoup

from trendstorm.shared.errors import ParseError
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.agents.scout.fetcher import FetchResult
    from trendstorm.agents.scout.parsers import ParseResult

logger = get_logger(__name__)


def parse_html(result: FetchResult) -> ParseResult:
    """Extract main-body text from an HTML page."""
    from trendstorm.agents.scout.parsers import ParseResult  # avoid circular at module load

    html = result.raw_bytes.decode(result.encoding or "utf-8", errors="replace")

    text = trafilatura.extract(html, include_tables=True, include_links=False) or ""
    meta = trafilatura.extract_metadata(html)
    title: str | None = meta.title if meta else None
    language: str | None = meta.language if meta else None

    if not text:
        logger.debug("trafilatura_fallback", url=result.url)
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        if not title:
            title_tag = soup.find("title")
            if title_tag:
                title = title_tag.get_text(strip=True)

    if not text.strip():
        raise ParseError(
            "No text content could be extracted from HTML",
            context={"url": result.url, "source_id": result.source_id},
        )

    return ParseResult(
        text=text,
        title=title,
        language=language,
        char_count=len(text),
        word_count=len(text.split()),
    )
