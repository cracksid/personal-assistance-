"""
WebSocket endpoint for chat streaming.

REST (the /health endpoint) is request-in, response-out: the client asks
once, the server answers once, done. A chat conversation with an LLM needs
something different -- the server should be able to push text to the
client as it's generated (token by token), and the client should be able
to send new messages at any time, all over one long-lived connection.
That's what a WebSocket is: a persistent two-way pipe between client and
server, instead of one-shot request/response.

There's no AI here yet -- Phase 5 builds the actual agent loop. For now
this endpoint only proves the plumbing works: it accepts a connection,
echoes back whatever text it receives, and logs connect/disconnect. Phase
5 will replace the echo with a real call into the agent loop.
"""

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["chat"])
logger = logging.getLogger(__name__)


@router.websocket("/ws/chat")
async def chat_websocket(websocket: WebSocket) -> None:
    # accept() completes the WebSocket "handshake" -- until this is called,
    # the connection is still just a pending HTTP request.
    await websocket.accept()
    logger.info("WebSocket connected")

    try:
        # This loop runs once per message, for as long as the client stays
        # connected. `await websocket.receive_text()` pauses this function
        # (without blocking the rest of the server) until the client sends
        # something.
        while True:
            message = await websocket.receive_text()
            logger.info("Received: %s", message)
            await websocket.send_text(f"echo: {message}")
    except WebSocketDisconnect:
        # Raised automatically when the client closes the connection --
        # this is the normal way this loop ends, not an error.
        logger.info("WebSocket disconnected")
