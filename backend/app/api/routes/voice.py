"""
REST endpoints for speech-to-text and text-to-speech.

REST rather than WebSocket because these are one-shot request/response
operations: hand over a clip, get back a transcript. The WebSocket in
chat.py exists because a streamed reply arrives in many pieces over time;
nothing here does.

    POST /voice/transcribe   raw audio bytes  -> {"text": ..., "language": ...}
    POST /voice/speak        {"text": "..."}  -> audio/wav

/transcribe takes the raw request body rather than a multipart file upload.
Multipart would need the python-multipart package, and for a single-user
local API it buys nothing -- both `curl --data-binary` and the browser's
fetch() can post a file's bytes directly.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.deps import get_stt, get_tts
from app.errors import AppError
from app.providers.speech import (
    STTProvider,
    SpeechProviderError,
    Transcript,
    TTSProvider,
)

router = APIRouter(prefix="/voice", tags=["voice"])
logger = logging.getLogger(__name__)

# A minute of 16kHz mono WAV is about 2MB. 25MB is generous for a spoken
# command while still refusing an accidental upload of something enormous.
MAX_AUDIO_BYTES = 25 * 1024 * 1024


class SpeakRequest(BaseModel):
    """A Pydantic model, so FastAPI validates the body and documents it."""

    text: str = Field(min_length=1, max_length=5000)


@router.post("/transcribe")
async def transcribe(
    request: Request, stt: STTProvider = Depends(get_stt)
) -> Transcript:
    """
    Turn an uploaded audio clip into text.

    Returns an empty `text` when the clip contains no speech, or when the
    transcript was rejected as a likely hallucination. That is a success, not
    an error -- the caller simply has nothing to act on.
    """
    audio = await request.body()

    if not audio:
        raise AppError("Request body was empty; send audio bytes.", status_code=400)
    if len(audio) > MAX_AUDIO_BYTES:
        raise AppError(
            f"Audio is too large ({len(audio)} bytes, limit {MAX_AUDIO_BYTES}).",
            status_code=413,
        )

    try:
        # Transcription is CPU-bound and synchronous, so it goes on a worker
        # thread. Without this the whole server would freeze -- including
        # every open chat WebSocket -- for the seconds Whisper takes.
        transcript = await asyncio.to_thread(stt.transcribe, audio)
    except SpeechProviderError as exc:
        raise AppError(str(exc), status_code=422) from exc

    logger.info(
        "Transcribed %s bytes -> %s chars", len(audio), len(transcript.text)
    )
    return transcript


@router.post("/speak")
async def speak(body: SpeakRequest, tts: TTSProvider = Depends(get_tts)) -> Response:
    """Turn text into a WAV file the caller can play."""
    try:
        audio = await asyncio.to_thread(tts.synthesize, body.text)
    except SpeechProviderError as exc:
        raise AppError(str(exc), status_code=422) from exc

    logger.info("Synthesised %s chars -> %s bytes of audio", len(body.text), len(audio))

    # Returned as a raw body with an audio content type, so a browser can
    # play it directly and `curl -o out.wav` writes a usable file.
    return Response(content=audio, media_type="audio/wav")
