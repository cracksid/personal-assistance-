"""
Shared pytest fixtures.

conftest.py is a special filename: pytest imports it automatically and
makes any fixture defined here available to every test file in this
folder, without importing anything.
"""

from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models  # noqa: F401  (imported so Base.metadata knows the tables)
from app.db.base import Base


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """
    A database session backed by a throwaway in-memory database.

    A test asks for this by taking `db_session` as a parameter; pytest
    matches the name to this fixture and passes the yielded value in.

    Tests must never touch the real jarvis.db -- they would pollute real
    data, and their results would depend on whatever happened to be in it
    already. "sqlite://" with no path means "in memory": the database
    exists only in RAM and disappears when the engine is disposed, so
    every test starts from a guaranteed-clean schema.

    StaticPool forces every connection to reuse the *same* in-memory
    database. Without it, SQLAlchemy's pool could hand out a second
    connection, which for in-memory SQLite means a second, empty database.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # create_all() builds the tables directly from the models. Tests use
    # this rather than running migrations because it's much faster and
    # keeps tests independent of migration history. `alembic check`
    # (see README) is what verifies models and migrations still agree.
    Base.metadata.create_all(engine)

    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
