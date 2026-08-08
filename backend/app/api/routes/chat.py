"""
WebSocket endpoint for chat streaming.

A WebSocket is a persistent two-way pipe between client and server, unlike
REST's one-request-one-response. That matters here because the server pushes
the model's reply out piece by piece as it is generated, and because a turn
can pause to ask the user a question.

Client -> server. Plain text is treated as a user message. A JSON object is
treated as a control message:

    {"type": "confirm", "confirmation_id": "..."}   approve a pending tool
    {"type": "cancel",  "confirmation_id": "..."}   decline it

Server -> client. Every frame is a JSON object with a "type":

    {"type": "chunk",        "text": "Hel"}          part of the reply
    {"type": "tool",         "tool_name": ..., "ok": true, "output": ...}
    {"type": "confirmation", "confirmation_id": ..., "description": ...}
    {"type": "done"}                                 the turn is complete
    {"type": "error",        "message": "..."}

A "confirmation" frame means NOTHING HAS RUN. The turn has ended, and the
tool executes only if the client sends back a "confirm".
"""

import json
import logging

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.api.deps import get_agent, get_gate
from app.core.agent import Agent
from app.core.gate import ToolGate
from app.db import crud
from app.db.session import get_db
from app.providers.base import LLMProviderError

router = APIRouter(tags=["chat"])
logger = logging.getLogger(__name__)


@router.websocket("/ws/chat")
async def chat_websocket(
    websocket: WebSocket,
    db: Session = Depends(get_db),
    agent: Agent = Depends(get_agent),
    gate: ToolGate = Depends(get_gate),
) -> None:
    await websocket.accept()
    logger.info("WebSocket connected")

    # Resume the most recent conversation rather than starting a new one, so
    # closing the tab no longer wipes short-term context. Long-term memory
    # (the facts table) is separate and survives regardless.
    owner = crud.get_or_create_owner(db)
    conversation = crud.get_or_create_active_conversation(db, owner)

    try:
        while True:
            raw = await websocket.receive_text()
            control = _parse_control(raw)

            try:
                if control is not None:
                    await _handle_control(
                        websocket, db, gate, owner.id, conversation.id, control
                    )
                else:
                    await _handle_message(
                        websocket, db, agent, owner.id, conversation.id, raw
                    )

            except LLMProviderError as exc:
                # An expected failure -- no API key, rate limit, network down.
                # The message is written for a human and never contains the
                # API key, so it is safe to show.
                logger.warning("Provider error: %s", exc)
                await websocket.send_json({"type": "error", "message": str(exc)})

            except Exception:
                # An actual bug. Log the traceback server-side; tell the
                # client something generic, the same rule as the HTTP handlers.
                logger.error("Unexpected error handling message", exc_info=True)
                await websocket.send_json(
                    {"type": "error", "message": "Internal server error"}
                )

    except WebSocketDisconnect:
        # The normal way this loop ends: the client closed the tab.
        logger.info("WebSocket disconnected")


def _parse_control(raw: str) -> dict | None:
    """
    Return the control message this frame carries, or None if it is chat.

    Plain text stays plain text -- a user typing `{"hello"}` is a message,
    not a malformed command, so anything that is not a JSON object with a
    recognised "type" falls through to being treated as chat.
    """
    stripped = raw.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict) and parsed.get("type") in {"confirm", "cancel"}:
        return parsed
    return None


async def _handle_message(
    websocket: WebSocket,
    db: Session,
    agent: Agent,
    user_id: int,
    conversation_id: int,
    text: str,
) -> None:
    """Run one conversational turn and stream its events to the client."""
    logger.info("User message (%s chars)", len(text))

    async for event in agent.respond(db, user_id, conversation_id, text):
        if event.type == "text":
            await websocket.send_json({"type": "chunk", "text": event.text})
        elif event.type == "tool":
            await websocket.send_json(
                {
                    "type": "tool",
                    "tool_name": event.tool_name,
                    "ok": event.ok,
                    "output": event.text,
                }
            )
        elif event.type == "confirmation":
            await websocket.send_json(
                {
                    "type": "confirmation",
                    "confirmation_id": event.confirmation_id,
                    "tool_name": event.tool_name,
                    "description": event.description,
                }
            )

    await websocket.send_json({"type": "done"})

    # Fact extraction happens after "done" is sent, so the second model call
    # it makes never delays the user's reply. It swallows its own errors --
    # see Agent.remember.
    await agent.remember(db, user_id, conversation_id)


async def _handle_control(
    websocket: WebSocket,
    db: Session,
    gate: ToolGate,
    user_id: int,
    conversation_id: int,
    control: dict,
) -> None:
    """Approve or decline a tool the model asked to run."""
    confirmation_id = str(control.get("confirmation_id", ""))

    if control["type"] == "cancel":
        gate.cancel(confirmation_id)
        await _record(db, conversation_id, "The user declined that action.")
        await websocket.send_json(
            {"type": "tool", "tool_name": None, "ok": False, "output": "Cancelled."}
        )
        await websocket.send_json({"type": "done"})
        return

    result = await gate.confirm(db, confirmation_id, user_id=user_id)
    output = result.output or result.error or ""

    # Written into the conversation so the model sees, on the NEXT turn, that
    # the action happened. Deliberately not followed by another model call:
    # re-prompting purely to say "done" would cost a round trip and real
    # money for every confirmation, and the client already shows the result.
    await _record(db, conversation_id, f"(Tool result: {output})")

    await websocket.send_json(
        {"type": "tool", "tool_name": None, "ok": result.ok, "output": output}
    )
    await websocket.send_json({"type": "done"})


async def _record(db: Session, conversation_id: int, note: str) -> None:
    import asyncio

    await asyncio.to_thread(crud.add_message, db, conversation_id, "user", note)
