"""
REST endpoints for screenshots, image understanding, and OCR.

    GET  /vision/screenshot          -> image/png of the screen
    POST /vision/describe?prompt=... -> {"description": "..."}   (image body)
    POST /vision/ocr                 -> {"text": ..., "lines": [...]}
    POST /vision/screen?prompt=...   -> capture and describe, in one call

The last one exists because "what is on my screen?" is the question people
actually ask, and splitting it into two calls would ship a full screenshot
down to the client only to send it straight back up again.

As with /voice/transcribe, image endpoints read the raw request body rather
than a multipart upload -- that avoids adding python-multipart for no
benefit in a local single-user API.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from app.api.deps import get_ocr, get_vision
from app.errors import AppError
from app.providers import screen
from app.providers.vision import (
    OCRProvider,
    OCRResult,
    VisionProvider,
    VisionProviderError,
)

router = APIRouter(prefix="/vision", tags=["vision"])
logger = logging.getLogger(__name__)

# A 1568px PNG screenshot is typically 1-3MB. 20MB leaves room for a large
# photograph while refusing an accidental upload of something enormous.
MAX_IMAGE_BYTES = 20 * 1024 * 1024

DEFAULT_PROMPT = "Describe what is shown in this image, concisely."


class Description(BaseModel):
    description: str


async def _read_image(request: Request) -> bytes:
    image = await request.body()
    if not image:
        raise AppError("Request body was empty; send image bytes.", status_code=400)
    if len(image) > MAX_IMAGE_BYTES:
        raise AppError(
            f"Image is too large ({len(image)} bytes, limit {MAX_IMAGE_BYTES}).",
            status_code=413,
        )
    return image


@router.get("/screenshot")
async def screenshot(monitor: int = Query(1, ge=0)) -> Response:
    """Capture a monitor and return it as a PNG."""
    try:
        # Capture is fast (tens of milliseconds) but still blocking, so it
        # goes on a worker thread rather than stalling the event loop.
        image = await asyncio.to_thread(screen.capture_screen, monitor)
    except VisionProviderError as exc:
        raise AppError(str(exc), status_code=422) from exc

    logger.info("Captured monitor %s (%s bytes)", monitor, len(image))
    return Response(content=image, media_type="image/png")


@router.post("/describe")
async def describe(
    request: Request,
    prompt: str = Query(DEFAULT_PROMPT),
    vision: VisionProvider = Depends(get_vision),
) -> Description:
    """Ask the vision model a question about an uploaded image."""
    image = await _read_image(request)

    try:
        # Already async -- vision waits on a network, so no to_thread here.
        answer = await vision.describe(image, prompt)
    except VisionProviderError as exc:
        raise AppError(str(exc), status_code=422) from exc

    return Description(description=answer)


@router.post("/ocr")
async def ocr(
    request: Request, engine: OCRProvider = Depends(get_ocr)
) -> OCRResult:
    """
    Extract literal text from an uploaded image.

    Returns empty text when the image contains none -- a normal outcome
    rather than an error.
    """
    image = await _read_image(request)

    try:
        # OCR is local computation, so it does need a worker thread.
        result = await asyncio.to_thread(engine.read_text, image)
    except VisionProviderError as exc:
        raise AppError(str(exc), status_code=422) from exc

    logger.info("OCR found %s line(s)", len(result.lines))
    return result


@router.post("/screen")
async def describe_screen(
    prompt: str = Query(DEFAULT_PROMPT),
    monitor: int = Query(1, ge=0),
    vision: VisionProvider = Depends(get_vision),
) -> Description:
    """
    Capture the screen and describe it, in one call.

    This is the "what am I looking at?" endpoint -- the composition is here
    rather than in the client so a multi-megabyte screenshot never leaves
    the machine only to be sent straight back.
    """
    try:
        image = await asyncio.to_thread(screen.capture_screen, monitor)
        answer = await vision.describe(image, prompt)
    except VisionProviderError as exc:
        raise AppError(str(exc), status_code=422) from exc

    return Description(description=answer)
