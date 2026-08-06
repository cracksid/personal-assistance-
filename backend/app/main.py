"""
Entry point for the FastAPI application.

Run with:
    uvicorn app.main:app --reload

`app` below is the FastAPI application object -- Uvicorn imports this exact
name (app.main:app means "the `app` variable inside app/main.py") and uses
it to handle incoming HTTP and WebSocket connections.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.deps import get_memory_store
from app.api.router import api_router
from app.config import settings
from app.db.session import SessionLocal
from app.errors import register_exception_handlers
from app.logging_config import setup_logging
from app.middleware import log_requests

# Configure logging before anything else runs, so startup itself is logged.
setup_logging()

logger = logging.getLogger(__name__)


def _heal_memory_index() -> None:
    """
    Rebuild the vector index from the facts table if it has gone missing.

    The Chroma index is derived data -- it can be deleted, corrupted, or left
    behind when the database is restored from a backup. Without this check
    the symptom is silent: memory searches return nothing forever and the
    assistant simply seems to have forgotten everything, with no error.

    Only runs when the index is empty, so a healthy start costs one count().
    """
    try:
        store = get_memory_store()
        if store.count() > 0:
            return

        db = SessionLocal()
        try:
            rebuilt = store.rebuild_index(db)
            if rebuilt:
                logger.warning(
                    "Memory index was empty; rebuilt it from %s stored fact(s)",
                    rebuilt,
                )
        finally:
            db.close()

    except Exception:
        # Memory is an enhancement, never a reason the server won't start.
        logger.error("Could not check the memory index", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Startup and shutdown hooks.

    An async context manager: everything before `yield` runs once as the
    server starts, everything after runs once as it stops. FastAPI calls it
    for us because it is passed to FastAPI(lifespan=...) below.
    """
    _heal_memory_index()
    yield
    # Nothing to tear down yet -- SQLAlchemy and Chroma both clean up their
    # own connections when the process exits.


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.middleware("http")(log_requests)
register_exception_handlers(app)
app.include_router(api_router)
