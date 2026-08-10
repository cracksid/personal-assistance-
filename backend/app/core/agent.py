"""
THE AGENT LOOP.

Per CLAUDE.md this is the only part of the system that knows it is an
assistant. Everything below it is generic: providers just move text to and
from a model, memory just stores and finds facts, tools just do things.

The loop as of Phase 9b:

    save the user's message
      -> search long-term memory for relevant facts
      -> assemble the system prompt from those facts
      -> load recent messages (short-term memory)
      -> ask the model, offering it the tool list
      -> stream the reply out as it arrives
      -> if it asked for tools: run them THROUGH THE GATE, feed results back,
         and ask again  (up to agent_max_tool_steps times)
      -> if a tool needs confirmation: stop and ask the human
      -> save the finished reply
      -> afterwards: decide what was worth remembering, and store it

WHY CONFIRMATION ENDS THE TURN.

When the model asks for something destructive, this loop does not park a
half-finished coroutine waiting for a human to answer. It yields the
confirmation request and returns. The user approves through a separate call,
the gate runs the tool, and the result is fed back as a fresh turn.

Suspending mid-loop would mean holding a database session, an open provider
connection, and partially-built history in memory for however long someone
takes to decide -- possibly never. Ending the turn keeps every wait
explicit and every resource released.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.core.extraction import extract_facts
from app.core.gate import ConfirmationRequired, ToolGate
from app.core.prompts import build_system_prompt
from app.db import crud
from app.memory.store import MemoryStore
from app.providers.base import ChatMessage, LLMProvider, ToolSpec
from app.tools import registry

logger = logging.getLogger(__name__)


class AgentEvent(BaseModel):
    """
    One thing the user should see while a turn is being produced.

    A single model with optional fields rather than a class hierarchy,
    because these go straight out as JSON over the WebSocket and a flat
    shape is far easier for a client to switch on.
    """

    type: Literal["text", "tool", "confirmation"]

    # type="text": a piece of the reply, as it arrives.
    text: str = ""

    # type="tool": a tool that has already run.
    tool_name: str | None = None
    ok: bool | None = None

    # type="confirmation": nothing has run; a human must approve first.
    confirmation_id: str | None = None
    description: str | None = None


def tool_specs() -> list[ToolSpec]:
    """
    Describe every registered tool in the provider-neutral form.

    This lives in core/ rather than in tools/ because it is the one place
    allowed to see both sides: tools/ must not import providers/, and
    providers/ must not import tools/. Core sits above both and joins them.

    model_json_schema() turns each tool's Pydantic input model into JSON
    Schema, which is exactly what both Anthropic and Ollama want. The tool
    author writes a Python class; the model receives a schema.
    """
    return [
        ToolSpec(
            name=tool.name,
            description=tool.description,
            input_schema=tool.input_schema.model_json_schema(),
        )
        for tool in registry.all_tools()
    ]


class Agent:
    """Runs one conversation turn from user input to streamed reply."""

    def __init__(
        self,
        provider: LLMProvider,
        memory: MemoryStore,
        gate: ToolGate | None = None,
    ) -> None:
        """
        Collaborators are passed in rather than built here -- "constructor
        injection", the same dependency-injection idea as FastAPI's Depends.

        The provider's type is LLMProvider, the abstract base class, so this
        file cannot depend on anything Claude- or Ollama-specific.

        `gate` is optional, and an Agent without one simply cannot run tools.
        That keeps the object constructible in tests that only care about
        chat, and makes "no gate" fail by having no tools rather than by
        running them ungated.
        """
        self._provider = provider
        self._memory = memory
        self._gate = gate

    def _tools_available(self) -> list[ToolSpec] | None:
        """The tool list to offer the model, or None to offer none."""
        if not settings.tools_enabled:
            return None
        if self._gate is None or not self._provider.supports_tools:
            return None
        specs = tool_specs()
        return specs or None

    async def respond(
        self,
        db: Session,
        user_id: int,
        conversation_id: int,
        user_text: str,
        unattended: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """
        Handle one user message, yielding events as the turn is produced.

        asyncio.to_thread(...) runs a *synchronous* function on a separate
        thread and awaits its result. The database and Chroma calls are both
        synchronous, and calling one directly from async code would block the
        whole server until it finished.

        `unattended` marks a turn nobody is watching -- a scheduled task
        firing at 8am. It changes exactly one thing: the gate refuses
        destructive tools instead of asking. The agent does not decide that
        itself; it passes the flag down and the gate enforces it, because
        CLAUDE.md puts that decision in exactly one place.
        """
        # Save the user's message FIRST, then load history. Because of that
        # order the history below already contains this message, and it is
        # persisted even if the model call fails a moment later.
        await asyncio.to_thread(
            crud.add_message, db, conversation_id, "user", user_text
        )

        # Long-term memory: which known facts relate to what was just asked?
        facts = await asyncio.to_thread(self._memory.search, user_id, user_text)
        system = build_system_prompt(facts)

        # Short-term memory: the recent back-and-forth.
        history = await asyncio.to_thread(crud.get_recent_messages, db, conversation_id)
        messages = [ChatMessage(role=row.role, content=row.content) for row in history]

        specs = self._tools_available()
        logger.info(
            "Agent turn: conversation=%s history=%s messages, %s facts, %s tools",
            conversation_id,
            len(messages),
            len(facts),
            len(specs) if specs else 0,
        )

        reply_parts: list[str] = []
        awaiting_confirmation = False

        for step in range(settings.agent_max_tool_steps):
            turn_text: list[str] = []
            tool_calls = []

            async for event in self._provider.stream_turn(messages, system, specs):
                if event.type == "text" and event.text:
                    turn_text.append(event.text)
                    reply_parts.append(event.text)
                    yield AgentEvent(type="text", text=event.text)
                elif event.type == "end":
                    tool_calls = event.tool_calls

            if not tool_calls:
                break

            logger.info(
                "Model requested %s tool(s): %s",
                len(tool_calls),
                ", ".join(c.name for c in tool_calls),
            )

            # Record what the model asked for, so the next request shows it
            # its own previous decision. Kept in memory only -- the audit log
            # is the durable record of tool activity.
            messages.append(
                ChatMessage(
                    role="assistant",
                    content="".join(turn_text),
                    tool_calls=tool_calls,
                )
            )

            for call in tool_calls:
                # EVERY tool call goes through the gate. There is no path
                # from here to a tool's run() that skips it.
                outcome = await self._gate.invoke(
                    db,
                    call.name,
                    call.arguments,
                    user_id=user_id,
                    unattended=unattended,
                )

                if isinstance(outcome, ConfirmationRequired):
                    yield AgentEvent(
                        type="confirmation",
                        confirmation_id=outcome.confirmation_id,
                        tool_name=outcome.tool_name,
                        description=outcome.description,
                    )
                    awaiting_confirmation = True
                    # `continue`, not `break`. A model can request several
                    # tools at once, and stopping here would silently drop
                    # the rest -- observed live: asked for delete_file and
                    # create_reminder together, the delete needed approval
                    # and the reminder was never set, with nothing said.
                    #
                    # So the safe calls still run and every dangerous one
                    # still gets its own confirmation. The turn ends after
                    # this loop, once they have all been dealt with.
                    continue

                yield AgentEvent(
                    type="tool", tool_name=call.name, ok=outcome.ok, text=outcome.output
                )
                messages.append(
                    ChatMessage(
                        role="tool",
                        tool_call_id=call.id,
                        tool_name=call.name,
                        content=outcome.output or outcome.error or "(no output)",
                    )
                )

            if awaiting_confirmation:
                break
        else:
            # The for-loop finished without break: the model kept asking for
            # tools until the cap. Say so rather than ending in silence.
            logger.warning(
                "Tool loop hit the %s-step cap", settings.agent_max_tool_steps
            )
            yield AgentEvent(
                type="text",
                text="\n\n(I stopped after several tool steps without reaching an answer.)",
            )

        reply = "".join(reply_parts)
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
            # facts we have.
            known = await asyncio.to_thread(self._memory.search, user_id, user_text, 10)

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
