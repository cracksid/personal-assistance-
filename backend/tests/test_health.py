"""
Tests for the /health endpoint.

TestClient wraps our FastAPI `app` so we can send it fake requests directly
in Python, without starting a real Uvicorn server on a real port.
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_check_returns_ok():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
