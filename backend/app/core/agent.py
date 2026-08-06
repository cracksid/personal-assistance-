"""
THE AGENT LOOP.

Per CLAUDE.md this is the only part of the system that knows it is an
assistant. Everything below it is generic: providers just move text to and
from a model, memory just stores and finds facts, the database just stores
rows.

The loop as of Phase 6:

    save the user's message
      -> search long-term memory for relevant facts
      -> assemble the system prompt from those facts
      -> load recent messages (short-term memory)
      -> ask the model
      -> stream the reply out as it arrives
      -> save the finished reply
      -> afterwards: decide what was worth remembering, and store it

Not yet built (Phase 9, when tools exist): the model choosing a tool call,
the confirmation gate, executing the tool, and feeding the result back.
"""

import asyncio
import logging
from collections.abc import AsyncIterator

from sqlalchemy.orm import Session

from app.core.extraction import extract_facts
from app.core.prompts import build_system_prompt
from app.db import crud
from app.memory.store import MemoryStore
from app.providers.base import ChatMessage, LLMProvider

logger = logging.getLogger(__name__)


class Agent:
    """Runs one conversation turn from user input to streamed reply."""

    def __init__(self, provider: LLMProvider, memory: MemoryStore) -> None:
        """
        Both collaborators are passed in rather than built here --
        "constructor injection", the same dependency-injection idea as
        FastAPI's Depends.

        The provider's type is LLMProvider, the abstract base class, so this
        file cannot depend on anything Claude- or Ollama-specific. It also
        means tests can pass fakes and exercise the whole loop with no
        network call, no API key, and no vector index on disk.
        """
        self._provider = provider
        self._memory = memory

    async def respond(
        self, db: Session, user_id: int, conversation_id: int, user_text: str
    ) -> AsyncIterator[str]:
        """
        Handle one user message, yielding the reply chunk by chunk.

        asyncio.to_thread(...) runs a *synchronous* function on a separate
        thread and awaits its result. The database and Chroma calls are both
        synchronous, and calling one directly from async code would block the
        whole server until it finished -- no other request or WebSocket would
        be served in the meantime.
        """
        # Save the user's message FIRST, then load history. Because of that
        # order the history below already contains this message, and it is
        # persisted even if the model call fails a moment later.
        await asyncio.to_thread(crud.add_message, db, conversation_id, "user", user_text)

        # Long-term memory: which known facts relate to what was just asked?
        # Searching on the user's own words is what makes "what do I use for
        # sound?" surface a fact about sounddevice.
        facts = await asyncio.to_thread(self._memory.search, user_id, user_text)
        system = build_system_prompt(facts)

        # Short-term memory: the recent back-and-forth.
        history = await asyncio.to_thread(crud.get_recent_messages, db, conversation_id)
        messages = [ChatMessage(role=row.role, content=row.content) for row in history]

        logger.info(
            "Agent turn: conversation=%s history=%s messages, %s facts recalled",
            conversation_id,
            len(messages),
            len(facts),
        )

        # Collect the chunks as they stream past so the finished reply can be
        # saved, while yielding each one immediately so the user sees text
        # appear right away rather than after a long silence.
        chunks: list[str] = []
        async for chunk in self._provider.stream_chat(messages, system):
            chunks.append(chunk)
            yield chunk

        reply = "".join(chunks)
        if reply:
            await asyncio.to_thread(
                crud.add_message, db, conversation_id, "assistant", reply
            )

    async def remember(self, db: Session, user_id: int, conversation_id: int) -> None:
        """
        Look at the exchange just completed and store anything durable.

        Called AFTER the reply has been streamed, so the second model call it
        makes never delays the user's answer.

        Every failure is swallowed and logged. Memory is an enhancement: a
        confused extractor or a dropped connection must cost at most a
        forgotten fact, never a visible error after a reply the user already
        received successfully.
        """
        try:
            exchange = await asyncio.to_thread(
                crud.get_last_exchange, db, conversation_id
            )
            if exchange is None:
                return

            user_text, assistant_text = exchange

            # Show the extractor what we already know about this topic, so it
            # reports only genuinely new information instead of rewording
            # facts we have. A wider limit than the chat prompt uses: here
            # coverage of near-misses matters more than token economy, since
            # a fact we fail to show is a duplicate we invite.
            known = await asyncio.to_thread(
                self._memory.search, user_id, user_text, 10
            )

            facts = await extract_facts(
                self._provider, user_text, assistant_text, known
            )

            # Log the count even when it's zero. Without this line, "the
            # extractor found nothing" and "extraction never ran" look
            # identical in the logs -- which cost real time to debug once.
            logger.info("Extraction found %s candidate fact(s)", len(facts))

            for fact in facts:
                await asyncio.to_thread(
                    self._memory.remember,
                    db,
                    user_id,
                    fact.content,
                    fact.kind,
                    conversation_id,
                )

        except Exception:
            logger.error("Fact extraction failed", exc_info=True)
