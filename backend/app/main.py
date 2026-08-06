"""
Entry point for the FastAPI application.

Run with:
    uvicorn app.main:app --reload

`app` below is the FastAPI application object -- Uvicorn imports this exact
name (app.main:app means "the `app` variable inside app/main.py") and uses
it to handle incoming HTTP and WebSocket connections.
"""

from fastapi import FastAPI

from app.api.router import api_router
from app.config import settings
from app.errors import register_exception_handlers
from app.logging_config import setup_logging
from app.middleware import log_requests

# Configure logging before anything else runs, so startup itself is logged.
setup_logging()

app = FastAPI(title=settings.app_name)

app.middleware("http")(log_requests)
register_exception_handlers(app)
app.include_router(api_router)
