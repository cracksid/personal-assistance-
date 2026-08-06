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


def test_websocket_streams_chunks_then_done(client: TestClient):
    with client.websocket_connect("/ws/chat") as websocket:
        websocket.send_text("hi")

        assert websocket.receive_json() == {"type": "chunk", "text": "Hel"}
        assert websocket.receive_json() == {"type": "chunk", "text": "lo"}
        assert websocket.receive_json() == {"type": "done"}


def test_websocket_reports_provider_failure_without_closing(
    failing_client: TestClient,
):
    with failing_client.websocket_connect("/ws/chat") as websocket:
        websocket.send_text("hi")
        response = websocket.receive_json()

        assert response["type"] == "error"
        assert "ANTHROPIC_API_KEY" in response["message"]

        # The connection stays open after an error, so the user can retry
        # without reloading the page.
        websocket.send_text("again")
        assert websocket.receive_json()["type"] == "error"
