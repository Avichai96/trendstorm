"""Output sanitization — strip XSS vectors from analyst text before persistence.

Used in two places:
    1. Before persisting Analysis.summary / Insight.claim text to Mongo.
    2. Before rendering HTML/PDF reports in the Publisher.

All functions are pure (no I/O, no side effects) and deterministic.
They operate on raw text, not structured HTML — the goal is to prevent
injected chunk content from becoming executable in a report renderer.

What we strip:
    - <script>...</script> blocks (including multiline)
    - Inline event handlers: on*="..." attributes
    - javascript: URI schemes
    - data: URI schemes (can carry executable payloads)
    - <iframe>, <object>, <embed>, <link>, <meta> tags (renderer-dangerous)

What we do NOT strip:
    - Regular HTML markup (<b>, <i>, <p>, etc.) — reports use Markdown-to-HTML
      rendering with controlled templates; inline formatting is expected.
    - URLs to public HTTPS resources — those are validated by the SSRF layer.

HTML escaping for report rendering:
    html_escape(text) is the safe primitive for embedding user-controlled
    text inside HTML attributes or unstructured text nodes. Use it in
    templates instead of raw interpolation.
"""
from __future__ import annotations

import html
import re

# ---------------------------------------------------------------------------
# Compiled patterns (module-level for efficiency)
# ---------------------------------------------------------------------------

# <script ...>...</script> including multiline content
_SCRIPT_TAG = re.compile(
    r"<script\b[^>]*>.*?</script\s*>",
    re.IGNORECASE | re.DOTALL,
)

# Event handler attributes: on*="..." or on*='...' or on*=handler
_EVENT_HANDLER_ATTR = re.compile(
    r"""\bon\w+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]*)""",
    re.IGNORECASE,
)

# javascript: URI scheme (in href, src, action, etc.)
_JAVASCRIPT_URI = re.compile(
    r"""(href|src|action|formaction|data)\s*=\s*["']?\s*javascript:""",
    re.IGNORECASE,
)

# data: URI scheme — can carry HTML, SVG, JavaScript payloads
_DATA_URI = re.compile(
    r"""(href|src|action|formaction)\s*=\s*["']?\s*data:""",
    re.IGNORECASE,
)

# Dangerous structural tags — remove the tag but keep inner text for <iframe>,
# strip entirely for void elements
_DANGEROUS_TAGS = re.compile(
    r"<\s*/?\s*(iframe|object|embed|applet|base|link|meta|noscript|form)"
    r"\b[^>]*>",
    re.IGNORECASE,
)

# <style>...</style> blocks (CSS can load external resources or contain expressions)
_STYLE_TAG = re.compile(
    r"<style\b[^>]*>.*?</style\s*>",
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sanitize_text(text: str) -> str:
    """Strip known XSS/injection vectors from analyst output text.

    Safe to call on arbitrary string content. Never raises. Returns a
    cleaned version of the input; non-dangerous content passes through
    unchanged.
    """
    if not text:
        return text

    result = text
    result = _SCRIPT_TAG.sub("", result)
    result = _STYLE_TAG.sub("", result)
    result = _DANGEROUS_TAGS.sub("", result)
    result = _EVENT_HANDLER_ATTR.sub("", result)
    result = _JAVASCRIPT_URI.sub(r"\1=", result)
    result = _DATA_URI.sub(r"\1=", result)
    return result


def html_escape(text: str) -> str:
    """HTML-escape user-controlled text for safe embedding in HTML templates.

    Use this in report templates whenever inserting analysis text, chunk
    excerpts, or other user-influenced content into HTML attribute values
    or text nodes. The stdlib html.escape is correct; this wrapper documents
    the intent and makes call sites grep-able.
    """
    return html.escape(text, quote=True)
