"""Report renderer — converts Analysis to Markdown/PDF/JSON.

Design:
    Markdown is the canonical source of truth. PDF and JSON are derived.
    - Markdown: Jinja2 template in services/publish/templates/report.md.j2
    - PDF: Markdown → HTML (via markdown lib) → PDF (via weasyprint)
    - JSON: Analysis.model_dump(mode="json") — structured output for API clients

Why Jinja2 for the template?
    Prompts live in .md files; reports should too. Jinja2 gives us loops and
    filters without embedding Python logic in templates. Template is content,
    not code — iterative tuning without code review overhead.

weasyprint notes:
    - Requires system fonts. The publisher Dockerfile installs fonts-dejavu.
    - weasyprint 63+ requires Pango/HarfBuzz. Added to publisher.Dockerfile.
    - CSS is not required for basic Markdown → PDF; we rely on browser defaults.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

from trendstorm.domain.analyses.models import Analysis
from trendstorm.shared.logging import get_logger

logger = get_logger(__name__)

_TEMPLATE_DIR = pathlib.Path(__file__).parent / "templates"


def _load_template() -> Any:
    import jinja2

    loader = jinja2.FileSystemLoader(str(_TEMPLATE_DIR))
    env = jinja2.Environment(loader=loader, autoescape=False)  # noqa: S701
    return env.get_template("report.md.j2")


class RenderEngine:
    """Renders Analysis objects to report formats.

    All methods are synchronous (weasyprint uses sync HTML→PDF).
    """

    def render_markdown(self, analysis: Analysis, *, category_name: str) -> str:
        """Render the analysis to Markdown via the Jinja2 template.

        Returns a UTF-8 Markdown string.
        """
        template = _load_template()
        return str(template.render(
            analysis=analysis,
            category_name=category_name,
        ))

    def render_pdf(self, markdown_text: str) -> bytes:
        """Render Markdown → HTML → PDF.

        Returns raw PDF bytes.
        """
        import markdown as md_lib  # type: ignore[import-untyped]
        import weasyprint  # type: ignore[import-untyped]

        html_body = md_lib.markdown(
            markdown_text,
            extensions=["extra", "sane_lists"],
        )
        # Minimal HTML wrapper so weasyprint has a valid document.
        html = (
            "<!DOCTYPE html>"
            "<html><head>"
            "<meta charset='utf-8'>"
            "<style>"
            "body { font-family: sans-serif; max-width: 800px; margin: 2em auto; }"
            "h1 { border-bottom: 2px solid #333; }"
            "h2 { border-bottom: 1px solid #999; }"
            "blockquote { border-left: 3px solid #aaa; padding-left: 1em; color: #555; }"
            "code { background: #f4f4f4; padding: 0.1em 0.3em; border-radius: 3px; }"
            "</style>"
            "</head>"
            f"<body>{html_body}</body>"
            "</html>"
        )
        pdf_bytes: bytes = weasyprint.HTML(string=html).write_pdf()
        return pdf_bytes

    def render_json(self, analysis: Analysis) -> bytes:
        """Serialize the Analysis to a pretty-printed JSON blob.

        Returns UTF-8 encoded bytes.
        """
        data: dict[str, Any] = analysis.model_dump(mode="json")
        return json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
