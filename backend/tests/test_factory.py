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
from app.providers.factory import (
    get_llm_provider,
    get_ocr_provider,
    get_stt_provider,
    get_tts_provider,
    get_vision_provider,
)
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


@pytest.mark.parametrize(
    "setting, builder, valid",
    [
        ("stt_provider", get_stt_provider, "whisper"),
        ("tts_provider", get_tts_provider, "piper"),
        ("vision_provider", get_vision_provider, "anthropic"),
        ("ocr_provider", get_ocr_provider, "rapidocr"),
    ],
)
def test_every_factory_refuses_an_unknown_name_and_says_what_is_valid(
    monkeypatch, setting, builder, valid
):
    """
    Written as one parametrised test across all five factories rather than
    five near-identical ones, because the property being asserted is the
    same in each case and a sixth factory added later should be one line
    here.

    A typo in .env is the likeliest way to hit this, so the message has to
    name both what was asked for and what exists -- "Unknown provider" alone
    sends someone reading source code.
    """
    monkeypatch.setattr(settings, setting, "nonsense-engine")

    with pytest.raises(LLMProviderError) as excinfo:
        builder()

    message = str(excinfo.value)
    assert "nonsense-engine" in message
    assert valid in message
