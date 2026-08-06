"""
Tests for the agent loop and the provider abstraction.

The key piece here is FakeProvider: a second implementation of LLMProvider
that yields canned text. It proves the abstract base class actually works as
an abstraction, and it lets the whole loop be tested with no network call, no
API key, and no cost.
"""

from collections.abc import AsyncIterator

import pytest

from app.core.agent import Agent
from app.db import crud
from app.providers.base import ChatMessage, LLMProvider


class FakeProvider(LLMProvider):
    """A provider that returns fixed text and records what it was asked."""

    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.received_messages: list[ChatMessage] = []
        self.received_system: str = ""

    async def stream_chat(
        self, messages: list[ChatMessage], system: str
    ) -> AsyncIterator[str]:
        self.received_messages = messages
        self.received_system = system
        for chunk in self.chunks:
            yield chunk


def test_provider_missing_stream_chat_cannot_be_instantiated():
    """
    The point of an abstract base class: a subclass that forgets a required
    method fails loudly at construction, not deep inside a request.
    """

    class BrokenProvider(LLMProvider):
        pass  # never implements stream_chat

    with pytest.raises(TypeError):
        BrokenProvider()


@pytest.mark.anyio
async def test_agent_streams_chunks_and_saves_both_messages(db_session):
    owner = crud.get_or_create_owner(db_session)
    conversation = crud.create_conversation(db_session, owner)
    provider = FakeProvider(["Hello", ", ", "Sid"])
    agent = Agent(provider)

    # `async for` over the agent, collected into a list. This is what the
    # WebSocket handler does, one chunk per frame sent to the browser.
    chunks = [chunk async for chunk in agent.respond(db_session, conversation.id, "hi")]

    assert chunks == ["Hello", ", ", "Sid"]

    # Both sides of the turn are persisted, and the assistant's chunks were
    # reassembled into one message.
    saved = crud.get_recent_messages(db_session, conversation.id)
    assert [(m.role, m.content) for m in saved] == [
        ("user", "hi"),
        ("assistant", "Hello, Sid"),
    ]


@pytest.mark.anyio
async def test_agent_sends_prior_turns_back_to_the_model(db_session):
    """
    The model is stateless -- it only knows what we send it. This is the test
    that would catch "the assistant forgets everything you said".
    """
    owner = crud.get_or_create_owner(db_session)
    conversation = crud.create_conversation(db_session, owner)
    agent = Agent(FakeProvider(["ok"]))

    # First turn.
    async for _ in agent.respond(db_session, conversation.id, "my name is Sid"):
        pass

    # Second turn, with a fresh provider so we can inspect exactly what it got.
    provider = FakeProvider(["Sid"])
    agent = Agent(provider)
    async for _ in agent.respond(db_session, conversation.id, "what is my name?"):
        pass

    assert [(m.role, m.content) for m in provider.received_messages] == [
        ("user", "my name is Sid"),
        ("assistant", "ok"),
        ("user", "what is my name?"),
    ]
    assert "JARVIS" in provider.received_system


@pytest.mark.anyio
async def test_agent_does_not_save_an_empty_reply(db_session):
    owner = crud.get_or_create_owner(db_session)
    conversation = crud.create_conversation(db_session, owner)
    agent = Agent(FakeProvider([]))

    async for _ in agent.respond(db_session, conversation.id, "hi"):
        pass

    saved = crud.get_recent_messages(db_session, conversation.id)
    assert [m.role for m in saved] == ["user"]
