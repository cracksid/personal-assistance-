"""
Tests for the provider adapters -- the reshaping, not the network.

WHY THIS FILE EXISTS.

A coverage run put anthropic_provider at 25% and ollama_provider at 54%,
the two lowest numbers in the codebase. That matters more than the numbers
suggest, because of WHAT is untested: the translation between JARVIS's
neutral message format and each vendor's wire format.

That translation is the entire point of having providers behind an ABC, and
the two shapes could hardly be less alike. Anthropic wants tool results as
`tool_result` blocks grouped inside a following USER message; Ollama wants
them as their own role. Anthropic gives tool calls ids; Ollama gives none
and the adapter mints them. Get any of it wrong and the symptom is a模糊
API error at runtime, or worse, silently dropped context.

None of it needs a network. It is data in, data out.

The Ollama request path IS exercised end to end here, through httpx's
MockTransport and the seam the constructor already provides -- so the
argument parsing, the id synthesis and the error messages are all real code
paths, with a fake server.

Scope note: test_ollama_provider.py already covers plain streaming chat --
the system prompt going first, a missing model, a server that is not
running. This file covers the TOOL path and Anthropic's reshaping, which
nothing was exercising.
"""

import httpx
import pytest

from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import ChatMessage, LLMProviderError, ToolCall, ToolSpec
from app.providers.ollama_provider import OllamaProvider


def user(text: str) -> ChatMessage:
    return ChatMessage(role="user", content=text)


def assistant_calling(name: str, call_id: str = "c1", text: str = "") -> ChatMessage:
    return ChatMessage(
        role="assistant",
        content=text,
        tool_calls=[ToolCall(id=call_id, name=name, arguments={"path": "notes.txt"})],
    )


def tool_result(call_id: str, output: str) -> ChatMessage:
    return ChatMessage(
        role="tool", tool_call_id=call_id, tool_name="read_file", content=output
    )


# --- Anthropic's shape -----------------------------------------------------


def test_plain_messages_pass_through():
    out = AnthropicProvider._to_anthropic_messages([user("hello")])

    assert out == [{"role": "user", "content": "hello"}]


def test_an_assistant_tool_request_becomes_content_blocks():
    out = AnthropicProvider._to_anthropic_messages(
        [user("read it"), assistant_calling("read_file", text="Let me look.")]
    )

    blocks = out[1]["content"]
    assert out[1]["role"] == "assistant"
    assert blocks[0] == {"type": "text", "text": "Let me look."}
    assert blocks[1]["type"] == "tool_use"
    assert blocks[1]["name"] == "read_file"
    assert blocks[1]["id"] == "c1"


def test_a_tool_request_with_no_text_has_no_empty_text_block():
    """An empty text block is rejected by the API, not merely untidy."""
    out = AnthropicProvider._to_anthropic_messages([assistant_calling("read_file")])

    assert all(block["type"] != "text" for block in out[0]["content"])


def test_tool_results_become_a_user_message():
    """
    THE awkward part of Anthropic's format: a tool result is not its own
    role. It is a tool_result block inside a user message.
    """
    out = AnthropicProvider._to_anthropic_messages(
        [user("read it"), assistant_calling("read_file"), tool_result("c1", "hi")]
    )

    assert out[2]["role"] == "user"
    assert out[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "c1", "content": "hi"}
    ]


def test_consecutive_tool_results_are_grouped_into_one_message():
    """
    Emitting them as separate messages is rejected by the API. This is the
    case that appears whenever the model asks for two tools at once, which
    it does routinely.
    """
    messages = [
        user("do both"),
        ChatMessage(
            role="assistant",
            tool_calls=[
                ToolCall(id="c1", name="read_file", arguments={}),
                ToolCall(id="c2", name="list_directory", arguments={}),
            ],
        ),
        tool_result("c1", "first"),
        tool_result("c2", "second"),
    ]

    out = AnthropicProvider._to_anthropic_messages(messages)

    results = [m for m in out if m["role"] == "user" and isinstance(m["content"], list)]
    assert len(results) == 1
    assert [block["tool_use_id"] for block in results[0]["content"]] == ["c1", "c2"]


def test_an_empty_tool_result_is_not_sent_as_an_empty_string():
    """The API rejects empty content, and "it ran and said nothing" is real."""
    out = AnthropicProvider._to_anthropic_messages(
        [assistant_calling("read_file"), tool_result("c1", "")]
    )

    assert out[-1]["content"][0]["content"] == "(no output)"


# --- Ollama's shape --------------------------------------------------------


def test_ollama_tool_calls_are_openai_shaped():
    out = OllamaProvider()._to_ollama_messages([assistant_calling("read_file")], "sys")

    call = out[1]["tool_calls"][0]
    assert call == {"function": {"name": "read_file", "arguments": {"path": "notes.txt"}}}
    # No id is sent: Ollama matches results to calls by position.
    assert "id" not in call


def test_ollama_tool_results_are_their_own_role():
    """The opposite of Anthropic, which buries them in a user message."""
    out = OllamaProvider()._to_ollama_messages([tool_result("c1", "hi")], "sys")

    assert out[1] == {"role": "tool", "content": "hi"}


# --- the Ollama request path, against a fake server ------------------------


def fake_ollama(payload: dict, status: int = 200):
    """A transport that answers /api/chat with `payload`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


SPEC = ToolSpec(name="read_file", description="Read a file.", input_schema={})


async def collect(provider: OllamaProvider, tools=None):
    return [event async for event in provider.stream_turn([user("go")], "sys", tools)]


@pytest.mark.anyio
async def test_a_tool_call_is_read_back_out():
    transport = fake_ollama(
        {
            "message": {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "read_file", "arguments": {"path": "a.txt"}}}
                ],
            }
        }
    )

    events = await collect(OllamaProvider(transport=transport), [SPEC])

    assert events[-1].type == "end"
    assert events[-1].tool_calls[0].name == "read_file"
    assert events[-1].tool_calls[0].arguments == {"path": "a.txt"}


@pytest.mark.anyio
async def test_ids_are_synthesised_because_ollama_sends_none():
    """
    The agent pairs a result with the call it answers by id, so an adapter
    for an API that has no ids has to invent them.
    """
    transport = fake_ollama(
        {
            "message": {
                "tool_calls": [
                    {"function": {"name": "read_file", "arguments": {}}},
                    {"function": {"name": "list_directory", "arguments": {}}},
                ]
            }
        }
    )

    events = await collect(OllamaProvider(transport=transport), [SPEC])

    assert [call.id for call in events[-1].tool_calls] == ["call_0", "call_1"]


@pytest.mark.anyio
async def test_arguments_arriving_as_a_json_string_are_parsed():
    """Some Ollama builds do this. Accept both rather than crash on one."""
    transport = fake_ollama(
        {
            "message": {
                "tool_calls": [
                    {"function": {"name": "read_file", "arguments": '{"path": "b.txt"}'}}
                ]
            }
        }
    )

    events = await collect(OllamaProvider(transport=transport), [SPEC])

    assert events[-1].tool_calls[0].arguments == {"path": "b.txt"}


@pytest.mark.anyio
async def test_unparseable_arguments_become_empty_rather_than_crashing():
    """
    A malformed argument string should cost one bad tool call, not the
    whole turn. The gate rejects the empty arguments straight afterwards.
    """
    transport = fake_ollama(
        {"message": {"tool_calls": [{"function": {"name": "read_file", "arguments": "{oh no"}}]}}
    )

    events = await collect(OllamaProvider(transport=transport), [SPEC])

    assert events[-1].tool_calls[0].arguments == {}


@pytest.mark.anyio
async def test_text_alongside_a_tool_call_is_still_shown():
    transport = fake_ollama(
        {
            "message": {
                "content": "Let me check that.",
                "tool_calls": [{"function": {"name": "read_file", "arguments": {}}}],
            }
        }
    )

    events = await collect(OllamaProvider(transport=transport), [SPEC])

    assert events[0].type == "text"
    assert events[0].text == "Let me check that."


@pytest.mark.anyio
async def test_a_server_error_becomes_a_readable_message():
    """
    Callers never import httpx, so every failure has to arrive as our own
    exception type with a message written for a human.
    """
    transport = fake_ollama({"error": "model not found"}, status=404)

    with pytest.raises(LLMProviderError):
        await collect(OllamaProvider(transport=transport), [SPEC])


@pytest.mark.anyio
async def test_no_tools_means_the_streaming_path_is_used():
    """
    Ordinary chat must still arrive word by word. Only the tool path gives
    that up, and only because Ollama reports tool calls at the end anyway.
    """
    lines = (
        b'{"message":{"content":"Hel"},"done":false}\n'
        b'{"message":{"content":"lo"},"done":true}\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=lines)

    events = await collect(OllamaProvider(transport=httpx.MockTransport(handler)))

    assert [e.text for e in events if e.type == "text"] == ["Hel", "lo"]
    assert events[-1].type == "end"
