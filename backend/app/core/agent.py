"""
THE AGENT LOOP.

Per CLAUDE.md this is the only part of the system that knows it is an
assistant. Everything below it is generic: providers just move text to and
from a model, the database just stores rows.

The loop as built in Phase 5:

    save the user's message
      -> load recent history
      -> ask the model
      -> stream the reply out as it arrives
      -> save the finished reply

Not yet built (Phase 9, when tools exist): the model choosing a tool call,
the confirmation gate, executing the tool, and feeding the result back. The
shape below leaves room for those between "ask the model" and "stream out".
"""

import asyncio
import logging
from collections.abc import AsyncIterator

from sqlalchemy.orm import Session

from app.db import crud
from app.providers.base import ChatMessage, LLMProvider

logger = logging.getLogger(__name__)

# The assistant's standing instructions. Phase 6 will extend this with
# retrieved memories; Phase 9 will add the tool list.
SYSTEM_PROMPT = """You are JARVIS, a personal AI assistant running locally on \
the user's Windows machine.

Be direct and concise. Answer the question that was asked rather than \
restating it. When you are uncertain, say so plainly instead of hedging at \
length."""


class Agent:
    """Runs one conversation turn from user input to streamed reply."""

    def __init__(self, provider: LLMProvider) -> None:
        """
        The provider is passed in rather than built here -- "constructor
        injection", the same dependency-injection idea as FastAPI's Depends.

        The type hint is LLMProvider, the abstract base class, so this file
        cannot depend on anything Claude-specific. It also means tests can
        pass a fake provider and exercise the whole loop with no network
        call and no API key.
        """
        self._provider = provider

    async def respond(
        self, db: Session, conversation_id: int, user_text: str
    ) -> AsyncIterator[str]:
        """
        Handle one user message, yielding the reply chunk by chunk.

        asyncio.to_thread(...) runs a *synchronous* function on a separate
        thread and awaits its result. The database helpers are synchronous
        (see Phase 4), and calling one directly from async code would block
        the whole server until the query finished -- no other request or
        WebSocket would be served in the meantime.
        """
        # Save the user's message FIRST, then load history. Because of that
        # order, the history below already contains this message -- so it does
        # not have to be appended separately, and it is persisted even if the
        # model call fails a moment later.
        await asyncio.to_thread(crud.add_message, db, conversation_id, "user", user_text)

        history = await asyncio.to_thread(crud.get_recent_messages, db, conversation_id)

        # Convert database rows into the provider-neutral shape. The provider
        # then converts that into whatever its own API expects.
        messages = [ChatMessage(role=row.role, content=row.content) for row in history]

        logger.info(
            "Agent turn: conversation=%s history=%s messages",
            conversation_id,
            len(messages),
        )

        # Collect the chunks as they stream past so the finished reply can be
        # saved, while yielding each one immediately so the user sees text
        # appear right away rather than after a long silence.
        chunks: list[str] = []
        async for chunk in self._provider.stream_chat(messages, SYSTEM_PROMPT):
            chunks.append(chunk)
            yield chunk

        reply = "".join(chunks)
        if reply:
            await asyncio.to_thread(
                crud.add_message, db, conversation_id, "assistant", reply
            )
