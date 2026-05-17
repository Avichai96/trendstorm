"""Multi-query expansion via a small LLM (Gemini Flash by default).

Why multi-query retrieval?
    A single query can fail to surface relevant chunks that use different
    vocabulary. Expanding to N sub-queries and unioning the results via RRF
    increases recall without multiplying cost proportionally — each sub-query
    hits the same indexes; the union is deduplicated by RRF.

Design:
    - Prompt is loaded from a markdown file at first use (importlib.resources).
      Never a Python string literal — prompts are content, not code.
    - The LLM is told to return one sub-query per line with no formatting.
    - Parsing strips common LLM-generated prefixes (numbers, bullets, dashes).
    - If the LLM returns fewer lines than requested, the original query is
      appended to guarantee at least one query in the output.
    - If the LLM is unavailable, the fallback is [original_query] — retrieval
      proceeds with one query instead of N. This is a warning, not an error.
"""
from __future__ import annotations

import importlib.resources
import re
from typing import TYPE_CHECKING

from opentelemetry import trace

from trendstorm.domain.llm.models import Message
from trendstorm.shared.logging import get_logger

if TYPE_CHECKING:
    from trendstorm.domain.llm.providers import ChatProvider

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__)

# Compiled once at import time — strips leading numbering / bullet chars.
_PREFIX_RE = re.compile(r"^\s*(?:\d+[.)]\s*|[-•*#]\s*)+")


def _load_prompt() -> str:
    """Load the query expansion system prompt from its markdown file."""
    pkg = importlib.resources.files("trendstorm.services.analysis.prompts")
    return (pkg / "query_expansion.md").read_text(encoding="utf-8").strip()


def _parse_sub_queries(raw: str, *, count: int) -> list[str]:
    """Extract sub-queries from an LLM response.

    Strips numbering/bullet prefixes, deduplicates, and returns at most `count`
    non-empty lines.
    """
    lines = []
    seen: set[str] = set()
    for line in raw.splitlines():
        cleaned = _PREFIX_RE.sub("", line).strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            lines.append(cleaned)
        if len(lines) >= count:
            break
    return lines


class QueryExpander:
    """Expands a single query into multiple sub-queries via a chat LLM.

    Args:
        chat_provider   — any ChatProvider implementation (Gemini, Anthropic, etc.)
        _prompt_text    — override the prompt for unit tests; leave None for production
                          (loads from services/analysis/prompts/query_expansion.md)

    """

    def __init__(
        self,
        chat_provider: ChatProvider,
        *,
        _prompt_text: str | None = None,
    ) -> None:
        self._provider = chat_provider
        self._prompt: str = _prompt_text if _prompt_text is not None else _load_prompt()

    async def expand(self, query: str, *, count: int = 3) -> list[str]:
        """Return up to `count` sub-queries for the given base query.

        Falls back to [query] if the LLM call fails — callers always receive
        at least the original query and can proceed with single-query retrieval.
        """
        with tracer.start_as_current_span("retrieval.query_expansion") as span:
            span.set_attribute("retrieval.query", query[:200])
            span.set_attribute("retrieval.expansion_count", count)

            sub_queries = await self._call_llm(query, count=count)
            span.set_attribute("retrieval.expanded_count", len(sub_queries))
            return sub_queries

    async def _call_llm(self, query: str, *, count: int) -> list[str]:
        messages = [
            Message(role="system", content=self._prompt),
            Message(
                role="user",
                content=f"Generate {count} sub-queries for: {query}",
            ),
        ]
        try:
            completion = await self._provider.complete(messages)
        except Exception as exc:
            logger.warning(
                "query_expansion_llm_failed",
                query=query[:100],
                error=str(exc),
            )
            return [query]

        sub_queries = _parse_sub_queries(completion.content, count=count)

        if not sub_queries:
            logger.warning(
                "query_expansion_empty_response",
                query=query[:100],
                raw_response=completion.content[:200],
            )
            return [query]

        # Always include at least the original — ensures recall is never worse
        # than single-query retrieval.
        if query not in sub_queries:
            sub_queries = [query, *sub_queries[:count - 1]]

        logger.debug(
            "query_expansion_done",
            original=query[:100],
            count=len(sub_queries),
            sub_queries=[q[:80] for q in sub_queries],
        )
        return sub_queries
