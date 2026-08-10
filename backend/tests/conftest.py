"""
Shared pytest fixtures.

conftest.py is a special filename: pytest imports it automatically and makes
any fixture defined here available to every test file in this folder, without
importing anything.
"""

from collections.abc import Generator

import chromadb
import pytest
from chromadb.config import Settings as ChromaSettings
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models  # noqa: F401  (imported so Base.metadata knows the tables)
from app.db.base import Base
from app.memory.store import MemoryStore


class _InertScheduler:
    """
    A scheduler that does nothing, for tests.

    See never_touch_the_real_database below for why this has to exist.
    """

    async def deliver_due(self) -> int:
        return 0

    async def run_due(self) -> int:
        return 0


@pytest.fixture(autouse=True)
def never_touch_the_real_database() -> Generator[None, None, None]:
    """
    Stop the WebSocket tests reaching the developer's actual jarvis.db.

    THIS FIXTURE EXISTS BECAUSE OF A REAL BUG, found the hard way.

    Every other fixture here is careful to use a throwaway in-memory
    database. But the chat WebSocket route also depends on get_scheduler(),
    and the tests only ever overrode get_db, get_agent and get_gate. So
    get_scheduler() built the real one, bound to SessionLocal -- which
    points at jarvis.db on disk.

    The consequence was not theoretical. A test connected a WebSocket, the
    route called deliver_due() as it does on every connect, the scheduler
    read the REAL database, found a genuine pending reminder, pushed it into
    the test socket and marked it delivered. A real reminder was destroyed by
    running the test suite, and an unrelated test failed with "expected
    confirmation, got reminder".

    It stayed invisible for a whole phase because it only shows up when the
    real database happens to have something pending -- which means it was
    also a test that passed or failed depending on the developer's own data.

    autouse=True means every test gets this whether it asks or not. That is
    the point: the next route that quietly depends on a real-database
    singleton should not be able to reintroduce this.
    """
    from app.api import deps
    from app.main import app

    app.dependency_overrides[deps.get_scheduler] = _InertScheduler
    app.dependency_overrides[deps.get_task_runner] = _InertScheduler
    try:
        yield
    finally:
        app.dependency_overrides.pop(deps.get_scheduler, None)
        app.dependency_overrides.pop(deps.get_task_runner, None)
        # Singletons built during a test must not leak into the next one.
        deps._scheduler = None
        deps._task_runner = None


@pytest.fixture
def anyio_backend() -> str:
    """
    Tells the anyio pytest plugin which async library to run async tests on.

    Tests marked @pytest.mark.anyio need an event loop to run in; this picks
    asyncio, the one in Python's standard library. Without this fixture those
    tests are skipped rather than run.
    """
    return "asyncio"


@pytest.fixture
def db_engine() -> Generator[Engine, None, None]:
    """
    A throwaway in-memory database with the schema already created.

    Tests must never touch the real jarvis.db -- they would pollute real data,
    and their results would depend on whatever happened to be in it already.
    "sqlite://" with no path means "in memory": the database exists only in
    RAM and disappears when the engine is disposed, so every test starts from
    a guaranteed-clean schema.

    StaticPool forces every connection to reuse the *same* in-memory database.
    Without it, SQLAlchemy's pool could hand out a second connection, which
    for in-memory SQLite means a second, empty database.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # create_all() builds the tables directly from the models. Tests use this
    # rather than running migrations because it's much faster and keeps tests
    # independent of migration history. `alembic check` (see README) is what
    # verifies models and migrations still agree.
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(db_engine: Engine) -> Generator[Session, None, None]:
    """
    A database session backed by the throwaway engine above.

    A test asks for this by taking `db_session` as a parameter; pytest matches
    the name to this fixture and passes the yielded value in.
    """
    TestSession = sessionmaker(bind=db_engine, expire_on_commit=False)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def memory_store() -> MemoryStore:
    """
    A memory store backed by an in-memory Chroma index.

    EphemeralClient keeps everything in RAM and discards it, so tests never
    read or write the real chroma_data/ directory on disk.

    The reset() call is load-bearing: Chroma caches client instances, so two
    EphemeralClient() calls with the same settings hand back the SAME
    in-memory instance. Without the reset, facts stored by one test are still
    there in the next one, and tests pass or fail depending on their order.
    """
    client = chromadb.EphemeralClient(
        settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True)
    )
    client.reset()
    return MemoryStore(chroma_client=client)
