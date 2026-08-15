"""
Tests for the /ws/chat WebSocket endpoint.

These are integration tests: they drive the real route, the real agent loop,
and a real (throwaway) database -- only the LLM provider is faked.

app.dependency_overrides is FastAPI's testing hook. It maps a dependency
function to a replacement, so `Depends(get_db)` in the route hands back the
test database instead of the real one, with no change to route code. This is
the payoff of declaring dependencies rather than constructing them inline.
"""

from collections.abc import AsyncIterator, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_agent
from app.core.agent import Agent
from app.db.session import get_db
from app.main import app
from app.memory.store import MemoryStore
from app.providers.base import ChatMessage, LLMProvider, LLMProviderError
from tests.test_agent import FakeProvider


class FailingProvider(LLMProvider):
    """A provider that always fails, to exercise the error path."""

    async def stream_chat(
        self, messages: list[ChatMessage], system: str
    ) -> AsyncIterator[str]:
        raise LLMProviderError("ANTHROPIC_API_KEY is not set.")
        yield ""  # unreachable, but its presence is what makes this a generator


def _client_with(
    provider: LLMProvider, db_session: Session, memory_store: MemoryStore
) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        # Hand out the session the fixture owns, and do NOT close it here.
        # Closing per-request raced with the engine being disposed at
        # teardown, producing "Cannot operate on a closed database" warnings
        # from a generator finalised late by the garbage collector. Letting
        # the db_session fixture own the lifecycle removes the race.
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_agent] = lambda: Agent(provider, memory_store)
    try:
        yield TestClient(app)
    finally:
        # Always clear: overrides live on the app object, so leaving them in
        # place would leak into every later test.
        app.dependency_overrides.clear()


@pytest.fixture
def client(
    db_session: Session, memory_store: MemoryStore
) -> Generator[TestClient, None, None]:
    yield from _client_with(FakeProvider(["Hel", "lo"]), db_session, memory_store)


@pytest.fixture
def failing_client(
    db_session: Session, memory_store: MemoryStore
) -> Generator[TestClient, None, None]:
    yield from _client_with(FailingProvider(), db_session, memory_store)


def hello(websocket) -> int:
    """
    Consume the frame every connection opens with, and return the thread id.

    The server announces which conversation you are in as soon as you
    connect, so a UI can label it and so "new chat" has something visible to
    change. Every test has to read it before whatever it actually cares
    about.
    """
    frame = websocket.receive_json()
    assert frame["type"] == "conversation"
    return frame["id"]


def test_websocket_streams_chunks_then_done(client: TestClient):
    with client.websocket_connect("/ws/chat") as websocket:
        hello(websocket)
        websocket.send_text("hi")

        assert websocket.receive_json() == {"type": "chunk", "text": "Hel"}
        assert websocket.receive_json() == {"type": "chunk", "text": "lo"}
        assert websocket.receive_json() == {"type": "done"}


def test_websocket_reports_provider_failure_without_closing(
    failing_client: TestClient,
):
    with failing_client.websocket_connect("/ws/chat") as websocket:
        hello(websocket)
        websocket.send_text("hi")
        response = websocket.receive_json()

        assert response["type"] == "error"
        assert "ANTHROPIC_API_KEY" in response["message"]

        # The connection stays open after an error, so the user can retry
        # without reloading the page.
        websocket.send_text("again")
        assert websocket.receive_json()["type"] == "error"


def test_new_chat_switches_to_a_fresh_conversation(client: TestClient):
    """
    The control that did not exist until Phase 13, and whose absence caused
    a real problem: resuming the newest conversation forever is right for
    "close the tab and come back" and wrong for "drop this thread". With no
    way to drop one, a stale detail in the history followed the user around
    and the model kept answering from it.
    """
    with client.websocket_connect("/ws/chat") as websocket:
        first = hello(websocket)

        websocket.send_text("remember this")
        while websocket.receive_json()["type"] != "done":
            pass

        websocket.send_text('{"type": "new"}')
        second = hello(websocket)

        assert second != first


def test_the_old_conversation_still_exists_after_a_new_chat(
    client: TestClient, db_session: Session
):
    """"New chat" starts a thread. It does not delete one."""
    from sqlalchemy import select

    from app.db.models import Conversation, Message

    with client.websocket_connect("/ws/chat") as websocket:
        first = hello(websocket)
        websocket.send_text("keep me")
        while websocket.receive_json()["type"] != "done":
            pass

        websocket.send_text('{"type": "new"}')
        hello(websocket)

    assert db_session.get(Conversation, first) is not None
    kept = db_session.scalars(
        select(Message).where(Message.conversation_id == first)
    ).all()
    assert any(m.content == "keep me" for m in kept)


def test_a_new_conversation_is_a_chat_not_a_task(client: TestClient, db_session: Session):
    """
    Otherwise the next connect would skip straight past it -- resuming only
    ever picks up kind="chat".
    """
    from app.db.models import Conversation

    with client.websocket_connect("/ws/chat") as websocket:
        hello(websocket)
        websocket.send_text('{"type": "new"}')
        created = hello(websocket)

    assert db_session.get(Conversation, created).kind == "chat"
