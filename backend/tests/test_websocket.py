"""
Tests for the /ws/chat WebSocket echo endpoint.

TestClient.websocket_connect drives the real endpoint code in-process --
no real network socket needed -- and gives us a context manager to send
and receive messages against it.
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_chat_websocket_echoes_message():
    with client.websocket_connect("/ws/chat") as websocket:
        websocket.send_text("hello")
        response = websocket.receive_text()

    assert response == "echo: hello"
