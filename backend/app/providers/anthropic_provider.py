"""
The Anthropic (Claude) adapter.

This is the ONLY module in the project that knows Claude exists. Everything
above it talks to the LLMProvider abstract class from base.py.
"""

import logging
from collections.abc import AsyncIterator

import anthropic

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

# A ceiling, not a target. You are billed for tokens actually generated, so a
# generous limit costs nothing -- it only stops a long answer being cut off
# mid-sentence. Note this budget covers the model's internal reasoning as well
# as the visible reply, which is why it isn't set close to a typical answer's
# length.
MAX_TOKENS = 64_000


class AnthropicProvider(LLMProvider):
    """Streams chat completions from Anthropic's Messages API."""

    supports_tools = True

    def __init__(self) -> None:
        self._model = settings.llm_model
        self._effort = settings.llm_effort
        self._client: anthropic.AsyncAnthropic | None = None

        # Log the model but NEVER the key. Even printing the whole settings
        # object would be safe here -- anthropic_api_key is a SecretStr that
        # renders as "**********" -- but the habit is what matters.
        logger.info(
            "Anthropic provider ready (model=%s, effort=%s)", self._model, self._effort
        )

    def _get_client(self) -> anthropic.AsyncAnthropic:
        """
        Build the API client on first use, not in __init__.

        Constructing this object must never fail: it is created by dependency
        injection, before the request handler runs, so an exception here would
        kill a WebSocket connection with no chance to send a readable message.
        Failing on first *use* instead means the error reaches the user.
        """
        if self._client is None:
            api_key = settings.anthropic_api_key.get_secret_value()
            if not api_key:
                raise LLMProviderError(
                    "ANTHROPIC_API_KEY is not set. Add it to your .env file "
                    "(see .env.example)."
                )
            # AsyncAnthropic is the async client -- its calls are awaited, so
            # waiting on the network doesn't block the rest of the server.
            self._client = anthropic.AsyncAnthropic(api_key=api_key)
        return self._client

    @staticmethod
    def _to_anthropic_messages(messages: list[ChatMessage]) -> list[dict]:
        """
        Reshape our neutral messages into Anthropic's content-block format.

        Two things make this more than a field rename:

        - An assistant turn that asked for tools becomes a list of blocks:
          optional text, then one `tool_use` block per call.
        - Tool RESULTS are not their own role in Anthropic's API. They are
          `tool_result` blocks inside a *user* message, and consecutive
          results must be grouped into one message. Emitting them
          separately is rejected by the API.
        """
        out: list[dict] = []
        pending_results: list[dict] = []

        def flush_results() -> None:
            if pending_results:
                out.append({"role": "user", "content": list(pending_results)})
                pending_results.clear()

        for message in messages:
            if message.role == "tool":
                pending_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id,
                        "content": message.content or "(no output)",
                    }
                )
                continue

            flush_results()

            if message.role == "assistant" and message.tool_calls:
                blocks: list[dict] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                for call in message.tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.id,
                            "name": call.name,
                            "input": call.arguments,
                        }
                    )
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": message.role, "content": message.content})

        flush_results()
        return out

    @staticmethod
    def _translate(exc: Exception) -> LLMProviderError:
        """Turn an SDK exception into ours, so callers never import anthropic."""
        if isinstance(exc, anthropic.AuthenticationError):
            return LLMProviderError(
                "Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env."
            )
        if isinstance(exc, anthropic.RateLimitError):
            return LLMProviderError(
                "Rate limited by Anthropic. Wait a moment and try again."
            )
        if isinstance(exc, anthropic.APIConnectionError):
            return LLMProviderError(
                "Could not reach Anthropic. Check your internet connection."
            )
        if isinstance(exc, anthropic.APIStatusError):
            return LLMProviderError(f"Anthropic API error: {exc.message}")
        return LLMProviderError(f"Anthropic call failed: {exc}")

    async def stream_turn(
        self,
        messages: list[ChatMessage],
        system: str,
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[TurnEvent]:
        """
        Stream one turn, and report any tools the model wants run.

        Text is yielded as it arrives, exactly as before -- tool use does not
        cost the streaming experience. The tool requests only become known
        once the turn completes, which is why they ride on the final event.
        """
        client = self._get_client()

        request: dict = {
            "model": self._model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": self._to_anthropic_messages(messages),
            "output_config": {"effort": self._effort},
        }
        if tools:
            request["tools"] = [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "input_schema": spec.input_schema,
                }
                for spec in tools
            ]

        try:
            async with client.messages.stream(**request) as stream:
                async for chunk in stream.text_stream:
                    yield TurnEvent(type="text", text=chunk)

                # The accumulated message carries the tool_use blocks, which
                # only exist once the turn is complete.
                final = await stream.get_final_message()

        except anthropic.APIError as exc:
            raise self._translate(exc) from exc

        calls = [
            ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
            for block in final.content
            if block.type == "tool_use"
        ]
        yield TurnEvent(type="end", tool_calls=calls)

    async def stream_chat(
        self, messages: list[ChatMessage], system: str
    ) -> AsyncIterator[str]:
        """
        Yield the model's reply one chunk at a time.

        `async with ... as stream` is an async context manager: it opens the
        HTTP connection, guarantees it gets closed when the block exits (even
        on an exception), and gives us the stream object in between. Same
        idea as `with open(...) as f` for files, but it can pause while
        waiting on the network.
        """
        client = self._get_client()
        try:
            async with client.messages.stream(
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=system,
                # Translate our neutral ChatMessage shape into the dicts the
                # Anthropic SDK expects. This translation is exactly the job
                # this adapter exists to do.
                messages=[{"role": m.role, "content": m.content} for m in messages],
                # How much the model deliberates before answering. Configured
                # in .env so tuning cost vs. quality never touches code.
                output_config={"effort": self._effort},
            ) as stream:
                # text_stream yields only the visible reply text. The model's
                # internal reasoning is not included, so it can't leak into
                # what the user sees.
                async for chunk in stream.text_stream:
                    yield chunk

        # Translate the SDK's exceptions into our own, so nothing above this
        # module needs to import anthropic to handle a failure. Ordered most
        # specific first -- Python takes the first matching `except`.
        except anthropic.AuthenticationError as exc:
            raise LLMProviderError(
                "Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env."
            ) from exc
        except anthropic.RateLimitError as exc:
            raise LLMProviderError(
                "Rate limited by Anthropic. Wait a moment and try again."
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMProviderError(
                "Could not reach Anthropic. Check your internet connection."
            ) from exc
        except anthropic.APIStatusError as exc:
            # exc.message is the API's description of the problem. It never
            # contains the key.
            raise LLMProviderError(f"Anthropic API error: {exc.message}") from exc
