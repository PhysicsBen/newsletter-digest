"""Tests for src/config.py — settings load from .env correctly."""
import os

import pytest


def test_settings_defaults():
    """Settings should load with sensible defaults even without a .env file."""
    # Import after env is set so pydantic-settings picks up overrides
    from src.config import Settings

    s = Settings()
    assert s.llm_model == "gemini/gemini-3-flash-preview"
    assert s.llm_thinking_level == "low"
    assert s.prompt_version == "v1.0"
    assert s.staleness_days == 30
    assert s.topic_similarity_threshold == 0.82
    assert s.token_budget == 6000
    assert "sqlite" in s.database_url


def test_settings_env_override(monkeypatch):
    """Environment variables should override defaults."""
    monkeypatch.setenv("LLM_MODEL", "gemini/gemini-2-flash")
    monkeypatch.setenv("TOKEN_BUDGET", "4000")

    from importlib import reload
    import src.config as config_module
    reload(config_module)

    s = config_module.Settings()
    assert s.llm_model == "gemini/gemini-2-flash"
    assert s.token_budget == 4000
