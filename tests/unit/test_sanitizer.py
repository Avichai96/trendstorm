"""Unit tests for the output sanitizer.

All tests are pure (no I/O). Table-driven to cover each XSS pattern class.
"""
from __future__ import annotations

import pytest

from trendstorm.infrastructure.security.sanitize import html_escape, sanitize_text


@pytest.mark.unit
class TestSanitizeText:
    def test_clean_text_passes_through(self) -> None:
        text = "AI safety research has grown 40% year-over-year."
        assert sanitize_text(text) == text

    def test_empty_string_returns_empty(self) -> None:
        assert sanitize_text("") == ""

    def test_removes_script_tag(self) -> None:
        result = sanitize_text('Trend: <script>alert("xss")</script> ongoing.')
        assert "<script>" not in result
        assert "alert" not in result

    def test_removes_multiline_script_tag(self) -> None:
        result = sanitize_text(
            "Before<script>\n  document.cookie='stolen';\n  fetch('evil.com');\n</script>After"
        )
        assert "<script>" not in result
        assert "document.cookie" not in result
        assert "Before" in result
        assert "After" in result

    def test_removes_event_handler_double_quotes(self) -> None:
        result = sanitize_text('<p onmouseover="evil()">hover me</p>')
        assert "onmouseover" not in result

    def test_removes_event_handler_single_quotes(self) -> None:
        result = sanitize_text("<img src=x onerror='alert(1)'>")
        assert "onerror" not in result

    def test_removes_javascript_href(self) -> None:
        result = sanitize_text('<a href="javascript:void(0)">link</a>')
        assert "javascript:" not in result

    def test_removes_javascript_src(self) -> None:
        result = sanitize_text('<script src="javascript:evil()"></script>')
        assert "javascript:" not in result

    def test_removes_data_uri_in_src(self) -> None:
        result = sanitize_text('<img src="data:image/png;base64,abc123">')
        assert "data:" not in result

    def test_removes_data_uri_in_href(self) -> None:
        result = sanitize_text('<a href="data:text/html,<h1>xss</h1>">click</a>')
        assert "data:" not in result

    def test_removes_iframe_tag(self) -> None:
        result = sanitize_text('<iframe src="evil.com"></iframe>')
        assert "<iframe" not in result

    def test_removes_object_tag(self) -> None:
        result = sanitize_text('<object data="evil.swf" type="application/x-shockwave-flash"></object>')
        assert "<object" not in result

    def test_removes_style_block(self) -> None:
        result = sanitize_text(
            '<style>body { background: url("javascript:alert(1)") }</style>'
        )
        assert "<style>" not in result
        assert "background" not in result

    def test_removes_meta_refresh(self) -> None:
        result = sanitize_text('<meta http-equiv="refresh" content="0; url=evil.com">')
        assert "<meta" not in result

    def test_preserves_legitimate_markdown(self) -> None:
        text = "## Section\n**bold** and _italic_ with a [link](https://example.com)."
        result = sanitize_text(text)
        assert result == text

    def test_case_insensitive_script(self) -> None:
        result = sanitize_text("<SCRIPT>evil()</SCRIPT>")
        assert "evil" not in result

    @pytest.mark.parametrize("payload,expected_absent", [
        ('<script>alert(1)</script>', 'alert'),
        ('<img onerror="xss()">', 'onerror'),
        ('<a href="javascript:xss()">x</a>', 'javascript:'),
        ('<iframe src="evil.com"></iframe>', '<iframe'),
    ])
    def test_table_driven_xss_patterns(self, payload: str, expected_absent: str) -> None:
        result = sanitize_text(payload)
        assert expected_absent not in result


@pytest.mark.unit
class TestHtmlEscape:
    def test_escapes_lt_gt(self) -> None:
        result = html_escape("<script>evil()</script>")
        assert "<" not in result
        assert ">" not in result
        assert "&lt;" in result
        assert "&gt;" in result

    def test_escapes_quotes(self) -> None:
        result = html_escape('He said "hello" & \'world\'')
        assert '"' not in result
        assert "&amp;" in result

    def test_plain_text_unchanged_except_special_chars(self) -> None:
        text = "Normal trend analysis text."
        assert html_escape(text) == text

    def test_ampersand_escaped(self) -> None:
        assert "&amp;" in html_escape("cats & dogs")
