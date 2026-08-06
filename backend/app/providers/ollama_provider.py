"""
The Ollama adapter -- local models, no API key, no cost.

Ollama runs a small HTTP server on your own machine (default port 11434) and
serves whatever models you have downloaded. Nothing leaves the computer.

This adapter exists mainly to prove the abstraction works: it speaks a
completely different wire protocol from the Anthropic one -- plain HTTP with
newline-delimited JSON instead of a vendor SDK -- yet both end up yielding
the same thing, plain strings, so app/core/agent.py cannot tell them apart.

No new dependency: httpx is already installed (the Anthropic SDK uses it).
"""

import json
import logging
from collections.abc import AsyncIterator

import httpx

from app.config import settings
from app.providers.base import ChatMessage, LLMProvider, LLMProviderError

logger = logging.getLogger(__name__)

# Local models are slow to start: the first request has to load several GB of
# weights into RAM, which can take 30+ seconds on a machine without a GPU.
# `read` is the wait for each piece of the response, not the whole reply, so a
# long generation won't trip it -- only a genuinely stuck server will.
TIMEOUT = httpx.Timeout(connect=5.0, read=180.0, write=10.0, pool=5.0)


class OllamaProvider(LLMProvider):
    """Streams chat completions from a locally running Ollama server."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base_url = settings.ollama_base_url.rstrip("/")
        self._model = settings.ollama_model

        # A seam for testing: tests pass a fake transport so the adapter can be
        # exercised without a real Ollama server running. Production code never
        # passes this, so the default real network transport is used.
        self._transport = transport

        logger.info(
            "Ollama provider ready (model=%s, url=%s)", self._model, self._base_url
        )

    async def stream_chat(
        self, messages: list[ChatMessage], system: str
    ) -> AsyncIterator[str]:
        # Anthropic takes the system prompt as its own parameter. Ollama has no
        # such field -- it expects a message with role "system" at the front of
        # the list. Reshaping like this is precisely the adapter's job.
        payload = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
        }

        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT, transport=self._transport
            ) as client:
                async with client.stream(
                    "POST", f"{self._base_url}/api/chat", json=payload
                ) as response:
                    if response.status_code != 200:
                        # The body hasn't been downloaded yet on a streaming
                        # response -- aread() pulls it so the error message can
                        # say what actually went wrong.
                        body = (await response.aread()).decode(errors="replace")
                        raise LLMProviderError(
                            self._explain_error(response.status_code, body)
                        )

                    # Ollama streams NDJSON: one complete JSON object per line,
                    # e.g. {"message": {"content": "Hel"}, "done": false}
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        data = json.loads(line)
                        if data.get("done"):
                            break
                        chunk = data.get("message", {}).get("content", "")
                        if chunk:
                            yield chunk

        except httpx.ConnectError as exc:
            raise LLMProviderError(
                f"Could not reach Ollama at {self._base_url}. "
                "Make sure the Ollama app is running."
            ) from exc
        except httpx.TimeoutException as exc:
            raise LLMProviderError(
                "Ollama took too long to respond. Local models are slow to "
                "load the first time -- try again, or use a smaller model."
            ) from exc

    def _explain_error(self, status_code: int, body: str) -> str:
        """Turn Ollama's HTTP error into something worth reading."""
        if status_code == 404:
            return (
                f"Ollama has no model named {self._model!r}. "
                f'Download it first with:  ollama pull {self._model}'
            )
        return f"Ollama error (HTTP {status_code}): {body[:200]}"
