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
from fastapi.staticfiles import StaticFiles

from app.api.deps import get_memory_store, get_scheduler, get_watcher_service
from app.api.router import api_router
from app.config import PROJECT_ROOT, settings
from app.db.session import SessionLocal
from app.errors import register_exception_handlers
from app.logging_config import setup_logging
from app.middleware import log_requests
from app.plugins.loader import load_plugins
from app import settings_store

# Configure logging before anything else runs, so startup itself is logged.
setup_logging()

logger = logging.getLogger(__name__)


def _apply_saved_settings() -> None:
    """
    Layer overrides saved from the settings page onto the .env values.

    Failure here must never stop startup: a bad stored value should leave
    JARVIS running on its .env configuration, which is exactly what the
    file is for.
    """
    try:
        db = SessionLocal()
        try:
            settings_store.apply_overrides(db)
        finally:
            db.close()
    except Exception:
        logger.error("Could not apply saved settings", exc_info=True)


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
    # Settings saved from the UI are layered over .env before anything reads
    # them, so a provider chosen last week is in force by the time the first
    # connection arrives.
    _apply_saved_settings()

    _heal_memory_index()

    # Plugins load here rather than at import, for two reasons. Importing
    # the registry must not execute third-party code as a side effect --
    # the test suite imports it constantly. And the built-in tools are
    # already registered by then, so a plugin cannot claim a built-in's
    # name: registry.register refuses to shadow, and the plugin is the one
    # that loses.
    load_plugins()

    # The scheduler needs a running event loop, which exists by the time
    # lifespan runs but not at import. Starting it here also means it stops
    # cleanly on shutdown instead of being killed mid-check.
    scheduler = get_scheduler()
    scheduler.start()

    # Started after the scheduler because it captures the running event loop
    # to hand filesystem events across from watchdog's own thread.
    watchers = get_watcher_service()
    await watchers.start()

    yield

    await watchers.stop()
    scheduler.shutdown()
    # SQLAlchemy and Chroma clean up their own connections on exit.


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.middleware("http")(log_requests)
register_exception_handlers(app)
app.include_router(api_router)


def _serve_built_frontend() -> None:
    """
    Serve frontend/dist, if it has been built.

    WHY THE BACKEND SERVES THE UI RATHER THAN ELECTRON LOADING FILES.

    The frontend deliberately contains no host and no port anywhere: it
    fetches "/settings" and opens a socket at "/ws/chat". In development
    Vite proxies those to this server. Loading the same files from a
    file:// URL in Electron would resolve them against the filesystem
    instead, and every one would fail.

    Serving them from here means the UI and the API share an origin in
    production exactly as they appear to in development, so the same code
    works in both with no build-time switch and no baked-in address.

    MOUNTED LAST, AND THAT IS LOAD-BEARING. A mount at "/" matches
    everything, so it has to come after api_router or it would swallow
    /tools, /settings and the WebSocket. FastAPI checks routes in the order
    they were added.

    html=True makes it serve index.html for "/", which is what a single
    page app needs.
    """
    dist = PROJECT_ROOT / "frontend" / "dist"
    if not (dist / "index.html").exists():
        # Normal in development, where Vite serves the UI on its own port.
        logger.info("No built frontend at %s -- API only", dist)
        return

    app.mount("/", StaticFiles(directory=dist, html=True), name="ui")
    logger.info("Serving the built frontend from %s", dist)


_serve_built_frontend()
