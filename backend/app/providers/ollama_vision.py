"""
Image understanding via a local Ollama vision model.

Free, offline, and private -- but noticeably slower and weaker than Claude.
On a CPU with no GPU, expect tens of seconds per image from moondream, the
smallest usable option.

Ollama takes images as base64 strings in an `images` array on the message,
which is a different shape from Anthropic's content blocks. Reshaping that
is exactly what this adapter exists to do.
"""

import base64
import logging

import httpx

from app.config import settings
from app.providers.vision import VisionProvider, VisionProviderError

logger = logging.getLogger(__name__)

# Vision on a CPU is slow, and the model has to load into RAM first. This is
# a per-chunk read timeout, not a whole-response one, so a long description
# will not trip it -- only a genuinely stuck server.
TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0)


class OllamaVisionProvider(VisionProvider):
    """Answers questions about images using a local Ollama model."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base_url = settings.ollama_base_url.rstrip("/")
        self._model = settings.ollama_vision_model
        # A testing seam: tests inject a fake transport so this can be
        # exercised with no Ollama server running.
        self._transport = transport
        logger.info(
            "Ollama vision ready (model=%s, url=%s)", self._model, self._base_url
        )

    async def describe(self, image: bytes, prompt: str) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    # Ollama's shape: base64 strings on the message itself,
                    # rather than Anthropic's typed content blocks.
                    "images": [base64.standard_b64encode(image).decode("ascii")],
                }
            ],
            "stream": False,
        }

        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT, transport=self._transport
            ) as client:
                response = await client.post(
                    f"{self._base_url}/api/chat", json=payload
                )

                if response.status_code != 200:
                    body = response.text
                    if response.status_code == 404:
                        raise VisionProviderError(
                            f"Ollama has no model named {self._model!r}. "
                            f"Download it with:  ollama pull {self._model}"
                        )
                    raise VisionProviderError(
                        f"Ollama error (HTTP {response.status_code}): {body[:200]}"
                    )

                data = response.json()

        except VisionProviderError:
            raise
        except httpx.ConnectError as exc:
            raise VisionProviderError(
                f"Could not reach Ollama at {self._base_url}. "
                "Make sure the Ollama app is running."
            ) from exc
        except httpx.TimeoutException as exc:
            raise VisionProviderError(
                "Ollama took too long. Vision models are slow on a CPU -- try "
                "again, or set VISION_PROVIDER=anthropic."
            ) from exc

        return data.get("message", {}).get("content", "").strip()
