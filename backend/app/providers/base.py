"""
The abstract interface every LLM adapter must implement.

An abstract base class (ABC) defines a contract without providing the
implementation. Methods marked @abstractmethod MUST be implemented by any
subclass -- Python refuses to create an instance of a subclass that misses
one, and it fails at construction time rather than halfway through a request.

This is the mechanism behind the CLAUDE.md rule "never hardcode a provider
name anywhere in core/". The agent loop imports LLMProvider (this abstract
type) and never imports a concrete adapter. Swapping Claude for a local
model becomes a new file in this folder plus one line in .env.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Literal

from pydantic import BaseModel


class ToolCall(BaseModel):
    """
    The model asking for a tool to be run.

    `id` is the provider's own identifier for this request. It matters
    because a model can ask for several tools at once, and each result must
    be handed back attached to the call it answers -- otherwise the model
    cannot tell which output belongs to which request.
    """

    id: str
    name: str
    arguments: dict


class ChatMessage(BaseModel):
    """
    One turn of a conversation, in a shape no provider owns.

    Every provider has its own message format. Translating to and from THIS
    shape is each adapter's job -- so the agent loop never has to know
    whether it is talking to Anthropic, OpenAI, or a local model.
    """

    role: str  # "user" | "assistant" | "tool"
    content: str = ""

    # Set on an assistant message that asked for tools. Carried in history so
    # the model can see what it previously requested.
    tool_calls: list[ToolCall] = []

    # Set on a "tool" message: which call this output answers.
    tool_call_id: str | None = None
    tool_name: str | None = None


class ToolSpec(BaseModel):
    """
    A tool, described for the model.

    This is the provider-neutral form. Each adapter reshapes it into
    whatever its API expects -- Anthropic wants `input_schema`, Ollama wants
    an OpenAI-style `function` wrapper.
    """

    name: str
    description: str
    input_schema: dict  # JSON Schema, from the Tool's Pydantic model


class TurnEvent(BaseModel):
    """
    One thing that happened while the model was producing a turn.

    Two kinds:
      type="text" -- a piece of visible reply, as it arrives
      type="end"  -- the turn finished; tool_calls says what it wants run

    Modelled as events rather than a return value because a turn is BOTH a
    stream (text appearing word by word) and a decision (which tools to
    call). An async generator can only yield, not return a value, so the
    decision arrives as the final event.
    """

    type: Literal["text", "end"] = "text"
    text: str = ""
    tool_calls: list[ToolCall] = []


class LLMProviderError(Exception):
    """
    Raised by an adapter when the underlying API call fails.

    Adapters translate their provider's specific exceptions into this one,
    so callers can handle failure without importing an SDK's error types --
    which would leak the provider's identity back into core/.
    """


class LLMProvider(ABC):
    """Base class for every LLM adapter."""

    # Whether this adapter can pass tools to its model. Declared rather than
    # detected so the agent can decide up front which path to take, instead
    # of discovering mid-conversation that tools were silently ignored.
    supports_tools: bool = False

    async def stream_turn(
        self,
        messages: list[ChatMessage],
        system: str,
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[TurnEvent]:
        """
        Produce one assistant turn: streamed text, then any tool requests.

        DELIBERATELY NOT ABSTRACT. The default implementation below adapts
        stream_chat and ignores `tools`, so every provider written before
        tools existed -- including the fakes in the test suite -- keeps
        working untouched. Adding an @abstractmethod here would have broken
        all of them at construction time.

        An adapter that can do tool use overrides this and sets
        supports_tools = True.
        """
        async for chunk in self.stream_chat(messages, system):
            yield TurnEvent(type="text", text=chunk)
        # No tool_calls: this provider cannot ask for tools.
        yield TurnEvent(type="end")

    @abstractmethod
    def stream_chat(
        self, messages: list[ChatMessage], system: str
    ) -> AsyncIterator[str]:
        """
        Send the conversation to the model and yield the reply piece by piece.

        Implementations are written as `async def` with `yield` -- an async
        generator. Calling an async generator function doesn't run its body;
        it returns an AsyncIterator, which is why this signature is a plain
        `def` returning AsyncIterator[str]. Callers use it like:

            async for chunk in provider.stream_chat(messages, system):
                ...

        Streaming rather than returning one big string is what lets text
        appear word-by-word in the UI instead of after a long silence.

        Args:
            messages: conversation history, oldest first.
            system: instructions that shape the assistant's behaviour.

        Raises:
            LLMProviderError: if the provider call fails.
        """
        raise NotImplementedError
