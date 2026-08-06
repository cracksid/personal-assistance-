"""
Application-wide error handling.

Two things live here:
1. AppError -- a base exception any part of the app can raise when
   something goes wrong in an *expected* way (bad input, not found, etc.)
   and wants control over the HTTP status code and message the client sees.
2. register_exception_handlers -- wires AppError, and any *unexpected*
   exception, into JSON responses, so a bug never leaks a raw Python
   traceback to the client. Called once from main.py.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    """
    Raise this (or a subclass of it) anywhere in the app to return a
    specific HTTP status code and message to the client, e.g.:

        raise AppError("Conversation not found", status_code=404)
    """

    def __init__(self, message: str, status_code: int = 400) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def register_exception_handlers(app: FastAPI) -> None:
    """
    An "exception handler" tells FastAPI: whenever this type of exception
    is raised anywhere while handling a request, catch it here instead of
    letting it crash the request, and turn it into this JSON response.
    """

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.warning("AppError on %s %s: %s", request.method, request.url.path, exc.message)
        return JSONResponse(status_code=exc.status_code, content={"error": exc.message})

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # exc_info=True logs the full traceback server-side. The client
        # never sees it -- just the generic message below.
        logger.error(
            "Unhandled error on %s %s", request.method, request.url.path, exc_info=True
        )
        return JSONResponse(status_code=500, content={"error": "Internal server error"})
