"""
Abstract interfaces for image understanding.

Two different jobs, deliberately kept apart:

  VisionProvider - ask a model what an image shows. Handles meaning:
                   "which window is focused?", "what is this diagram saying?"
  OCRProvider    - pull the literal text out of an image. Fast, cheap,
                   exact, and it does not interpret anything.

They are complementary. OCR is far quicker and reads dense text reliably,
but it has no idea what any of it means -- and it makes characteristic
mistakes on ambiguous glyphs (reading "8000" as "8oo0"). A vision model
understands context but is slower and costs money or CPU time. Real
questions about a screen often want both.

WHY VisionProvider IS ASYNC AND OCRProvider IS NOT.

Same rule as elsewhere in this codebase: vision analysis waits on a network
(Anthropic's API, or Ollama's local HTTP server), so `async` lets the server
serve other requests meanwhile. OCR is local computation with nothing to
wait for, so declaring it async would be a lie -- callers push it onto a
worker thread with asyncio.to_thread instead.
"""

from abc import ABC, abstractmethod

from pydantic import BaseModel


class VisionProviderError(Exception):
    """
    Raised when an image engine fails.

    Adapters translate their library's exceptions into this one, so callers
    never import an SDK to handle failure -- which would leak the engine's
    identity upward into core/.
    """


class OCRLine(BaseModel):
    """One run of text the OCR engine found, with its own confidence."""

    text: str
    confidence: float


class OCRResult(BaseModel):
    """Everything readable in an image."""

    # All lines joined, which is what a caller normally wants.
    text: str

    # The per-line breakdown, kept so a caller can drop low-confidence runs
    # rather than trusting the lot. Confidence is per line, not per image,
    # so averaging it away would discard the useful signal.
    lines: list[OCRLine] = []


class VisionProvider(ABC):
    """Answers questions about an image."""

    @abstractmethod
    async def describe(self, image: bytes, prompt: str) -> str:
        """
        Look at an image and answer `prompt` about it.

        Args:
            image: the bytes of an image file (PNG, JPEG). Bytes rather than
                a path because images arrive over HTTP or straight from a
                screen capture, and writing to disk to read back would be
                pointless.
            prompt: what to ask about it.

        Raises:
            VisionProviderError: if the call fails.
        """
        raise NotImplementedError


class OCRProvider(ABC):
    """Extracts literal text from an image."""

    @abstractmethod
    def read_text(self, image: bytes) -> OCRResult:
        """
        Read whatever text is in an image.

        Returns an empty result rather than raising when an image simply
        contains no text -- that is a normal outcome, not a failure.

        Raises:
            VisionProviderError: if the engine itself fails.
        """
        raise NotImplementedError
