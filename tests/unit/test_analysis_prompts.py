"""Smoke tests for the analyst/validator/query_expansion markdown prompts.

These tests pin the loading contract — if a prompt file is renamed, moved, or
emptied by accident, these tests fail loudly. They do NOT lock the prompt
content (prompts are tuned iteratively); they only verify presence and shape.
"""
from __future__ import annotations

import importlib.resources

import pytest

_REQUIRED_PROMPTS = [
    "query_expansion.md",
    "analyst_system.md",
    "validator_system.md",
]


@pytest.mark.unit
class TestAnalysisPrompts:
    @pytest.mark.parametrize("filename", _REQUIRED_PROMPTS)
    def test_prompt_exists_and_non_empty(self, filename: str) -> None:
        pkg = importlib.resources.files("trendstorm.services.analysis.prompts")
        text = (pkg / filename).read_text(encoding="utf-8").strip()
        assert len(text) > 100, f"{filename} is suspiciously short"

    def test_analyst_prompt_mentions_record_analysis_tool(self) -> None:
        pkg = importlib.resources.files("trendstorm.services.analysis.prompts")
        text = (pkg / "analyst_system.md").read_text(encoding="utf-8")
        # The Analyst service expects this exact tool name.
        assert "record_analysis" in text

    def test_validator_prompt_mentions_record_validation_tool(self) -> None:
        pkg = importlib.resources.files("trendstorm.services.analysis.prompts")
        text = (pkg / "validator_system.md").read_text(encoding="utf-8")
        # The Validator service expects this exact tool name.
        assert "record_validation" in text

    def test_validator_prompt_mentions_all_five_rubric_dimensions(self) -> None:
        pkg = importlib.resources.files("trendstorm.services.analysis.prompts")
        text = (pkg / "validator_system.md").read_text(encoding="utf-8").lower()
        for dimension in ["grounding", "faithfulness", "quality", "coverage", "specificity"]:
            assert dimension in text, f"validator prompt missing dimension: {dimension}"
