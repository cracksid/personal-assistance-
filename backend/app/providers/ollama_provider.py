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
from app.providers.base import (
    ChatMessage,
    LLMProvider,
    LLMProviderError,
    ToolCall,
    ToolSpec,
    TurnEvent,
)

logger = logging.getLogger(__name__)

# Local models are slow to start: the first request has to load several GB of
# weights into RAM, which can take 30+ seconds on a machine without a GPU.
# `read` is the wait for each piece of the response, not the whole reply, so a
# long generation won't trip it -- only a genuinely stuck server will.
TIMEOUT = httpx.Timeout(connect=5.0, read=180.0, write=10.0, pool=5.0)


class OllamaProvider(LLMProvider):
    """Streams chat completions from a locally running Ollama server."""

    # Ollama supports tool calling, but only for models trained for it
    # (llama3.2 yes, moondream no). Declaring True here means "this adapter
    # can pass tools"; whether the configured model uses them well is a
    # separate question, and measurably it uses them worse than Claude.
    supports_tools = True

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

    def _to_ollama_messages(
        self, messages: list[ChatMessage], system: str
    ) -> list[dict]:
        """
        Reshape our neutral messages into Ollama's format.

        Different from Anthropic in every detail. Ollama has no system
        parameter (it wants a message with role "system" at the front), it
        uses OpenAI-style `tool_calls` on the assistant message rather than
        content blocks, and tool results are their own role rather than
        blocks inside a user message.

        Reshaping exactly this is what an adapter is for -- and why core/
        can stay ignorant of which model it is talking to.
        """
        out: list[dict] = [{"role": "system", "content": system}]

        for message in messages:
            if message.role == "tool":
                # Ollama matches results to calls by position, not by id, so
                # the id we synthesised is not sent back.
                out.append({"role": "tool", "content": message.content})
            elif message.role == "assistant" and message.tool_calls:
                out.append(
                    {
                        "role": "assistant",
                        "content": message.content,
                        "tool_calls": [
                            {
                                "function": {
                                    "name": call.name,
                                    "arguments": call.arguments,
                                }
                            }
                            for call in message.tool_calls
                        ],
                    }
                )
            else:
                out.append({"role": message.role, "content": message.content})

        return out

    async def stream_turn(
        self,
        messages: list[ChatMessage],
        system: str,
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[TurnEvent]:
        """
        Produce one turn, with tools if any were given.

        Falls back to the streaming path when there are no tools, so ordinary
        chat still appears word by word. With tools it makes a single
        non-streaming request: Ollama only reports tool calls once the turn
        is complete, so streaming would buy nothing but complexity.
        """
        if not tools:
            async for chunk in self.stream_chat(messages, system):
                yield TurnEvent(type="text", text=chunk)
            yield TurnEvent(type="end")
            return

        payload = {
            "model": self._model,
            "messages": self._to_ollama_messages(messages, system),
            "stream": False,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.input_schema,
                    },
                }
                for spec in tools
            ],
        }

        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT, transport=self._transport
            ) as client:
                response = await client.post(
                    f"{self._base_url}/api/chat", json=payload
                )
                if response.status_code != 200:
                    raise LLMProviderError(
                        self._explain_error(response.status_code, response.text)
                    )
                data = response.json()

        except LLMProviderError:
            raise
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

        message = data.get("message", {})

        text = message.get("content", "")
        if text:
            yield TurnEvent(type="text", text=text)

        calls: list[ToolCall] = []
        for index, raw in enumerate(message.get("tool_calls") or []):
            function = raw.get("function", {})
            arguments = function.get("arguments", {})
            # Some builds return arguments as a JSON string rather than an
            # object. Accept both instead of crashing on the variant.
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            calls.append(
                ToolCall(
                    # Ollama does not give tool calls an id, so we mint one.
                    # It is only ever used on our side, to pair a result with
                    # the call it answers.
                    id=f"call_{index}",
                    name=function.get("name", ""),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )

        yield TurnEvent(type="end", tool_calls=calls)

    async def stream_chat(
        self, messages: list[ChatMessage], system: str
    ) -> AsyncIterator[str]:
        # Anthropic takes the system prompt as its own parameter. Ollama has no
        # such field -- it expects a message with role "system" at the front of
        # the list. Reshaping like this is precisely the adapter's job.
        payload = {
            "model": self._model,
            "messages": self._to_ollama_messages(messages, system),
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
