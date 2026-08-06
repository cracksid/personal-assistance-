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
