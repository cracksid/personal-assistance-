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

from pydantic import BaseModel


class ChatMessage(BaseModel):
    """
    One turn of a conversation, in a shape no provider owns.

    Every provider has its own message format. Translating to and from THIS
    shape is each adapter's job -- so the agent loop never has to know
    whether it is talking to Anthropic, OpenAI, or a local model.
    """

    role: str  # "user" or "assistant"
    content: str


class LLMProviderError(Exception):
    """
    Raised by an adapter when the underlying API call fails.

    Adapters translate their provider's specific exceptions into this one,
    so callers can handle failure without importing an SDK's error types --
    which would leak the provider's identity back into core/.
    """


class LLMProvider(ABC):
    """Base class for every LLM adapter."""

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
