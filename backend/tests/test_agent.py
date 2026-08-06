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
from app.providers.base import ChatMessage, LLMProvider, LLMProviderError


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


class FailingProvider(LLMProvider):
    """A provider that always fails, to exercise error paths."""

    async def stream_chat(
        self, messages: list[ChatMessage], system: str
    ) -> AsyncIterator[str]:
        raise LLMProviderError("provider is down")
        yield ""  # unreachable, but its presence is what makes this a generator


@pytest.fixture
def conversation(db_session):
    """An owner and a fresh conversation, the setup nearly every test needs."""
    owner = crud.get_or_create_owner(db_session)
    return owner, crud.create_conversation(db_session, owner)


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
async def test_agent_streams_chunks_and_saves_both_messages(
    db_session, memory_store, conversation
):
    owner, conv = conversation
    provider = FakeProvider(["Hello", ", ", "Sid"])
    agent = Agent(provider, memory_store)

    # `async for` over the agent, collected into a list. This is what the
    # WebSocket handler does, one chunk per frame sent to the browser.
    chunks = [
        chunk async for chunk in agent.respond(db_session, owner.id, conv.id, "hi")
    ]

    assert chunks == ["Hello", ", ", "Sid"]

    # Both sides of the turn are persisted, and the assistant's chunks were
    # reassembled into one message.
    saved = crud.get_recent_messages(db_session, conv.id)
    assert [(m.role, m.content) for m in saved] == [
        ("user", "hi"),
        ("assistant", "Hello, Sid"),
    ]


@pytest.mark.anyio
async def test_agent_sends_prior_turns_back_to_the_model(
    db_session, memory_store, conversation
):
    """
    The model is stateless -- it only knows what we send it. This is the test
    that would catch "the assistant forgets everything you said".
    """
    owner, conv = conversation
    agent = Agent(FakeProvider(["ok"]), memory_store)

    async for _ in agent.respond(db_session, owner.id, conv.id, "my name is Sid"):
        pass

    # Second turn, with a fresh provider so we can inspect exactly what it got.
    provider = FakeProvider(["Sid"])
    agent = Agent(provider, memory_store)
    async for _ in agent.respond(db_session, owner.id, conv.id, "what is my name?"):
        pass

    assert [(m.role, m.content) for m in provider.received_messages] == [
        ("user", "my name is Sid"),
        ("assistant", "ok"),
        ("user", "what is my name?"),
    ]
    assert "JARVIS" in provider.received_system


@pytest.mark.anyio
async def test_remembered_facts_are_injected_into_the_system_prompt(
    db_session, memory_store, conversation
):
    """
    Long-term memory in one test: a fact stored earlier is retrieved by
    meaning and placed in front of the model, without the user restating it.
    """
    owner, conv = conversation
    memory_store.remember(
        db_session, owner.id, "The user prefers dark mode in every editor"
    )
    provider = FakeProvider(["ok"])
    agent = Agent(provider, memory_store)

    async for _ in agent.respond(
        db_session, owner.id, conv.id, "what colour scheme should I set up?"
    ):
        pass

    assert "dark mode" in provider.received_system


@pytest.mark.anyio
async def test_remember_extracts_facts_and_makes_them_searchable(
    db_session, memory_store, conversation
):
    owner, conv = conversation
    crud.add_message(db_session, conv.id, "user", "I'm Sid, I live in Bangalore")
    crud.add_message(db_session, conv.id, "assistant", "Good to know.")

    # The provider stands in for the extractor here, returning the JSON a
    # real model would.
    extractor_output = '[{"content": "The user lives in Bangalore", "kind": "identity"}]'
    agent = Agent(FakeProvider([extractor_output]), memory_store)

    await agent.remember(db_session, owner.id, conv.id)

    assert memory_store.search(owner.id, "where is the user based?") == [
        "The user lives in Bangalore"
    ]


@pytest.mark.anyio
async def test_remember_never_raises_when_extraction_fails(
    db_session, memory_store, conversation
):
    """
    Memory is an enhancement. A failure here happens AFTER the user already
    received their reply, so it must stay silent rather than surface an error
    for a turn that visibly succeeded.
    """
    owner, conv = conversation
    crud.add_message(db_session, conv.id, "user", "hi")
    crud.add_message(db_session, conv.id, "assistant", "hello")
    agent = Agent(FailingProvider(), memory_store)

    await agent.remember(db_session, owner.id, conv.id)  # must not raise

    assert memory_store.search(owner.id, "anything") == []


@pytest.mark.anyio
async def test_agent_does_not_save_an_empty_reply(
    db_session, memory_store, conversation
):
    owner, conv = conversation
    agent = Agent(FakeProvider([]), memory_store)

    async for _ in agent.respond(db_session, owner.id, conv.id, "hi"):
        pass

    saved = crud.get_recent_messages(db_session, conv.id)
    assert [m.role for m in saved] == ["user"]
