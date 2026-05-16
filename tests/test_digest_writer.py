"""Tests for src/llm/digest_writer.py — LLM title and overview generation."""
from __future__ import annotations

from unittest.mock import patch

from src.llm.digest_writer import (
    _generate_overview,
    _generate_section_title,
)


def test_generate_section_title_calls_llm():
    with patch("src.llm.digest_writer.call_llm", return_value="  AI Breaks Math Records  ") as mock_llm:
        result = _generate_section_title(["Summary of a great breakthrough."], "fallback title")
    mock_llm.assert_called_once()
    assert result == "AI Breaks Math Records"


def test_generate_section_title_empty_summaries_returns_fallback():
    result = _generate_section_title([], "fallback title")
    assert result == "fallback title"


def test_generate_section_title_llm_failure_returns_fallback():
    with patch("src.llm.digest_writer.call_llm", side_effect=RuntimeError("API down")):
        result = _generate_section_title(["Some summary."], "my fallback")
    assert result == "my fallback"


def test_generate_section_title_strips_quotes():
    with patch("src.llm.digest_writer.call_llm", return_value='"Quoted Title"'):
        result = _generate_section_title(["Summary."], "fallback")
    assert result == "Quoted Title"


def test_generate_overview_calls_llm():
    items = [("AI solves math problem", 9.5), ("Mistral releases new model", 8.0)]
    with patch("src.llm.digest_writer.call_llm", return_value="This week was big for AI.") as mock_llm:
        result = _generate_overview(items)
    mock_llm.assert_called_once()
    assert result == "This week was big for AI."


def test_generate_overview_empty_items():
    result = _generate_overview([])
    assert result == ""


def test_generate_overview_llm_failure_returns_empty():
    items = [("Some title", 8.0)]
    with patch("src.llm.digest_writer.call_llm", side_effect=RuntimeError("timeout")):
        result = _generate_overview(items)
    assert result == ""


def test_generate_overview_prompt_includes_titles():
    items = [("Unique Title ABC", 9.0), ("Another Headline XYZ", 7.5)]
    captured = {}

    def fake_llm(messages, **_kwargs):
        captured["content"] = messages[0]["content"]
        return "Overview text."

    with patch("src.llm.digest_writer.call_llm", side_effect=fake_llm):
        _generate_overview(items)

    assert "Unique Title ABC" in captured["content"]
    assert "Another Headline XYZ" in captured["content"]
