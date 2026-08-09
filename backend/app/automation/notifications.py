"""
Delivering messages JARVIS starts, rather than ones you asked for.

Everything up to now has been request/response: you say something, JARVIS
answers. A reminder is the first thing that travels the other way -- the
server speaks first, unprompted.

That needs a way to find whoever is listening. This keeps a set of connected
WebSockets and pushes to all of them.

WHY DELIVERY REPORTS A COUNT.

broadcast() returns how many clients actually received the message, and the
caller uses that to decide whether the reminder is done. If nobody was
listening -- laptop closed, browser shut -- the reminder stays pending and
is delivered on next connect instead of being marked delivered into the
void. A notification nobody saw has not been delivered.
"""

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class Client(Protocol):
    """
    The bit of a WebSocket this module actually needs.

    A Protocol describes a shape rather than a base class: anything with a
    matching send_json is acceptable, no inheritance required. That keeps
    this module independent of FastAPI, and lets tests pass a plain object
    that records what it was sent.
    """

    async def send_json(self, data: dict) -> None: ...


class NotificationHub:
    """Tracks who is connected, and pushes messages to them."""

    def __init__(self) -> None:
        # A list rather than a set: WebSocket objects are not reliably
        # hashable across every server implementation, and the number of
        # clients here is at most a handful.
        self._clients: list[Client] = []

    def register(self, client: Client) -> None:
        if client not in self._clients:
            self._clients.append(client)
        logger.info("Notification client connected (%s total)", len(self._clients))

    def unregister(self, client: Client) -> None:
        if client in self._clients:
            self._clients.remove(client)
        logger.info("Notification client disconnected (%s left)", len(self._clients))

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def broadcast(self, payload: dict) -> int:
        """
        Send `payload` to every connected client.

        Returns the number that received it successfully. A client whose
        send fails is dropped rather than retried: it has gone away, and
        keeping it would mean every future broadcast pays for a dead socket.
        """
        if not self._clients:
            return 0

        delivered = 0
        broken: list[Client] = []

        for client in list(self._clients):
            try:
                await client.send_json(payload)
                delivered += 1
            except Exception:
                # Expected during a disconnect race -- the socket closed
                # between our check and our send. Not worth a traceback.
                logger.info("Dropping a client that could not be reached")
                broken.append(client)

        for client in broken:
            self.unregister(client)

        return delivered
