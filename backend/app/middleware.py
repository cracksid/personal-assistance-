"""
Logs every HTTP request: method, path, status code, and how long it took.

This is separate from the exception handlers in errors.py -- middleware
runs for *every* request (success or failure), the handlers only run when
something is raised.
"""

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


async def log_requests(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "%s %s -> %s (%.1fms)",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response
