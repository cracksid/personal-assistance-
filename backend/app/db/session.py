"""
Database engine and session management.

The engine is created once for the whole application. Sessions are
created per request and closed when that request finishes.
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

# The engine holds the connection pool. Creating it does NOT connect yet --
# connections are opened lazily, on first use.
#
# check_same_thread=False is SQLite-specific. SQLite normally refuses to
# let a connection be used from a thread other than the one that created
# it. FastAPI runs regular (non-async) route functions in a thread pool,
# so a session can legitimately land on a different thread than the one
# that opened it. Disabling the check is the documented way to use SQLite
# with FastAPI; SQLAlchemy's pool still ensures one connection is only
# used by one thread at a time.
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)

# sessionmaker is a factory: calling SessionLocal() produces a new Session.
#
# expire_on_commit=False keeps attributes readable after commit(). By
# default SQLAlchemy marks objects "expired" on commit, so touching
# user.username afterwards fires another SELECT -- and raises if the
# session is already closed. Off is friendlier and avoids surprise queries.
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency that hands a database session to a route and
    guarantees it gets closed afterwards.

    Used in a route like this:

        @router.get("/things")
        def list_things(db: Session = Depends(get_db)):
            ...

    This is a generator: `yield` gives the session to the route and pauses
    here. When the request finishes -- successfully or with an exception --
    execution resumes and the `finally` block closes the session. That is
    why a forgotten close can't happen: it isn't the route's job.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
