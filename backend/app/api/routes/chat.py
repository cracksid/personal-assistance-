"""
WebSocket endpoint for chat streaming.

A WebSocket is a persistent two-way pipe between client and server, unlike
REST's one-request-one-response. That matters here because the server pushes
the model's reply out piece by piece as it is generated, instead of making
the user wait for the whole thing.

Message protocol -- every frame the server sends is a JSON object with a
"type" field, so the client can tell chunks apart from completion and errors:

    {"type": "chunk", "text": "Hel"}   one piece of the reply
    {"type": "done"}                   the reply is complete
    {"type": "error", "message": "..."} something went wrong

The client sends plain text: whatever the user typed.
"""

import logging

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.api.deps import get_agent
from app.core.agent import Agent
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
            user_text = await websocket.receive_text()
            logger.info("User message (%s chars)", len(user_text))

            try:
                async for chunk in agent.respond(
                    db, owner.id, conversation.id, user_text
                ):
                    await websocket.send_json({"type": "chunk", "text": chunk})
                await websocket.send_json({"type": "done"})

                # Fact extraction happens after "done" is sent, so the second
                # model call it makes never delays the user's reply. It
                # swallows its own errors -- see Agent.remember.
                await agent.remember(db, owner.id, conversation.id)

            except LLMProviderError as exc:
                # An expected failure -- no API key, rate limit, network down.
                # The message is written for a human, and never contains the
                # API key, so it is safe to show.
                logger.warning("Provider error: %s", exc)
                await websocket.send_json({"type": "error", "message": str(exc)})

            except Exception:
                # An actual bug. Log the traceback server-side; tell the client
                # something generic, the same rule as the HTTP error handlers.
                logger.error("Unexpected error handling message", exc_info=True)
                await websocket.send_json(
                    {"type": "error", "message": "Internal server error"}
                )

    except WebSocketDisconnect:
        # The normal way this loop ends: the client closed the tab.
        logger.info("WebSocket disconnected")
