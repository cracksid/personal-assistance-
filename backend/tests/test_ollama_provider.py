"""
Tests for the Ollama adapter.

These run without Ollama installed or running. httpx.MockTransport replaces
the network layer with a function that returns whatever response we want, so
the adapter's real code -- request shaping, NDJSON parsing, error handling --
is exercised against fake HTTP.
"""

import json

import httpx
import pytest

from app.providers.base import ChatMessage, LLMProviderError
from app.providers.ollama_provider import OllamaProvider

# What a real Ollama stream looks like: one JSON object per line, with a
# final object carrying done=true.
FAKE_STREAM = (
    b'{"message":{"role":"assistant","content":"Hel"},"done":false}\n'
    b'{"message":{"role":"assistant","content":"lo"},"done":false}\n'
    b'{"message":{"role":"assistant","content":""},"done":true}\n'
)


@pytest.mark.anyio
async def test_streams_content_from_ndjson_lines():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=FAKE_STREAM)

    provider = OllamaProvider(transport=httpx.MockTransport(handler))

    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            [ChatMessage(role="user", content="hi")], "be helpful"
        )
    ]

    assert chunks == ["Hel", "lo"]


@pytest.mark.anyio
async def test_system_prompt_is_sent_as_the_first_message():
    """
    Anthropic takes the system prompt as a separate parameter; Ollama expects
    it as a message with role "system". This asserts the adapter does that
    reshaping -- the thing that lets core/ stay provider-agnostic.
    """
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, content=FAKE_STREAM)

    provider = OllamaProvider(transport=httpx.MockTransport(handler))

    async for _ in provider.stream_chat(
        [ChatMessage(role="user", content="hi")], "be helpful"
    ):
        pass

    assert captured["messages"] == [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hi"},
    ]
    assert captured["stream"] is True


@pytest.mark.anyio
async def test_missing_model_explains_how_to_fix_it():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model not found"})

    provider = OllamaProvider(transport=httpx.MockTransport(handler))

    with pytest.raises(LLMProviderError) as excinfo:
        async for _ in provider.stream_chat([ChatMessage(role="user", content="hi")], ""):
            pass

    assert "ollama pull" in str(excinfo.value)


@pytest.mark.anyio
async def test_server_not_running_gives_a_readable_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = OllamaProvider(transport=httpx.MockTransport(handler))

    with pytest.raises(LLMProviderError) as excinfo:
        async for _ in provider.stream_chat([ChatMessage(role="user", content="hi")], ""):
            pass

    assert "Ollama" in str(excinfo.value)
