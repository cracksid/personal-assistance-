"""
Abstract interfaces for speech-to-text and text-to-speech.

CLAUDE.md names STTProvider and TTSProvider alongside LLMProvider, for the
same reason: core/ must never learn which engine is in use. Swapping Whisper
for a cloud STT, or Piper for a different voice engine, is a new adapter plus
one line in .env.

WHY THESE ARE SYNCHRONOUS AND LLMProvider IS NOT.

LLMProvider.stream_chat is `async` because it waits on a network -- the CPU
is idle while bytes travel, so the event loop should be doing something else.
Whisper and Piper are the opposite: they are CPU-bound, computing rather than
waiting. Declaring them `async` would be a lie, because the coroutine would
hold the event loop exactly as long as a plain function does.

So they stay synchronous, and callers push them onto a worker thread with
asyncio.to_thread -- the same treatment the database gets.
"""

from abc import ABC, abstractmethod

from pydantic import BaseModel


class SpeechProviderError(Exception):
    """
    Raised when a speech engine fails.

    Adapters translate their library's exceptions into this one, so callers
    can handle failure without importing faster_whisper or piper -- which
    would leak the engine's identity upward.
    """


class Transcript(BaseModel):
    """
    The result of transcribing audio.

    The two probability fields are not decoration. Whisper hallucinates
    confident-sounding text from silence and background noise -- the
    notorious example being "Thank you for watching!" from a silent clip.
    Carrying its own confidence signals lets the adapter throw those away
    instead of feeding phantom commands into the agent loop.
    """

    text: str
    language: str = "en"

    # Whisper's estimate that the audio contains no speech at all. Near 1.0
    # means "this is silence"; near 0.0 means "someone is talking".
    no_speech_prob: float = 0.0

    # Average log-probability of the chosen words. Less negative is more
    # confident; strongly negative means the model was guessing.
    avg_logprob: float = 0.0


class STTProvider(ABC):
    """Turns spoken audio into text."""

    @abstractmethod
    def transcribe(self, audio: bytes) -> Transcript:
        """
        Transcribe an audio file held in memory.

        Args:
            audio: the bytes of an audio file (WAV, MP3, etc.), not raw
                samples. Bytes rather than a path because the audio usually
                arrives over HTTP or straight from a microphone buffer, and
                writing it to disk purely to read it back would be silly.

        Raises:
            SpeechProviderError: if transcription fails.
        """
        raise NotImplementedError


class TTSProvider(ABC):
    """Turns text into spoken audio."""

    @abstractmethod
    def synthesize(self, text: str) -> bytes:
        """
        Speak `text`, returning the bytes of a WAV file.

        WAV rather than MP3 because it needs no encoder, every browser and
        audio library reads it, and the files are short-lived anyway.

        Raises:
            SpeechProviderError: if synthesis fails.
        """
        raise NotImplementedError
