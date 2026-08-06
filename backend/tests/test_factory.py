"""
Tests for the provider factory.

This is the test for the architectural claim in CLAUDE.md: "switching models
is a config change". If these pass, changing one line in .env really does
swap the model, with no code edited anywhere.
"""

import pytest

from app.config import settings
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import LLMProviderError
from app.providers.factory import get_llm_provider
from app.providers.ollama_provider import OllamaProvider


def test_config_value_selects_the_provider(monkeypatch):
    """
    monkeypatch is a pytest fixture that changes an attribute for the duration
    of one test and puts it back afterwards -- so this test can pretend .env
    said something else without editing any file or affecting other tests.

    Note both providers construct fine with no API key and no Ollama server:
    adapters defer their real setup until the first call, so a misconfigured
    provider fails with a readable message instead of crashing at startup.
    """
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    assert isinstance(get_llm_provider(), AnthropicProvider)

    monkeypatch.setattr(settings, "llm_provider", "ollama")
    assert isinstance(get_llm_provider(), OllamaProvider)


def test_unknown_provider_name_lists_the_valid_ones(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "gpt9000")

    with pytest.raises(LLMProviderError) as excinfo:
        get_llm_provider()

    message = str(excinfo.value)
    assert "gpt9000" in message
    assert "anthropic" in message and "ollama" in message
