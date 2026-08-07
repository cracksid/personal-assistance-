"""
Image understanding via Claude.

Claude accepts images as content blocks alongside text in an ordinary
message, so this reuses the same SDK and API key as the chat adapter. There
is no separate vision endpoint to call.

This is one of only two modules that know Claude exists.
"""

import base64
import logging

import anthropic

from app.config import settings
from app.providers.vision import VisionProvider, VisionProviderError

logger = logging.getLogger(__name__)

# Descriptions are short; this is a ceiling that prevents a runaway response,
# not a target.
MAX_TOKENS = 4_000


class AnthropicVisionProvider(VisionProvider):
    """Answers questions about images using Claude."""

    def __init__(self) -> None:
        self._client: anthropic.AsyncAnthropic | None = None
        self._model = settings.vision_model
        logger.info("Anthropic vision ready (model=%s)", self._model)

    def _get_client(self) -> anthropic.AsyncAnthropic:
        """Built on first use so a missing key surfaces as a readable error."""
        if self._client is None:
            api_key = settings.anthropic_api_key.get_secret_value()
            if not api_key:
                raise VisionProviderError(
                    "ANTHROPIC_API_KEY is not set. Add it to .env, or set "
                    "VISION_PROVIDER=ollama to analyse images locally."
                )
            self._client = anthropic.AsyncAnthropic(api_key=api_key)
        return self._client

    async def describe(self, image: bytes, prompt: str) -> str:
        client = self._get_client()

        # The API carries image bytes as base64 inside JSON, since JSON has
        # no way to hold raw binary. Base64 costs about a third more bytes
        # on the wire -- the price of a text-only transport.
        encoded = base64.standard_b64encode(image).decode("ascii")

        try:
            response = await client.messages.create(
                model=self._model,
                max_tokens=MAX_TOKENS,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": encoded,
                                },
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            )

        except anthropic.AuthenticationError as exc:
            raise VisionProviderError(
                "Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env."
            ) from exc
        except anthropic.RateLimitError as exc:
            raise VisionProviderError(
                "Rate limited by Anthropic. Wait a moment and try again."
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise VisionProviderError(
                "Could not reach Anthropic. Check your internet connection."
            ) from exc
        except anthropic.APIStatusError as exc:
            raise VisionProviderError(f"Anthropic API error: {exc.message}") from exc

        # A response can contain several blocks; only the text ones matter.
        return "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
