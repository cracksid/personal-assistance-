"""
Tests for serving the built frontend.

Phase 14 has the backend serve frontend/dist so the Electron shell can load
http://127.0.0.1:8000 and have the UI and the API share an origin. The
thing that can go wrong is ordering: a mount at "/" matches everything, so
if it were added before the API routes it would swallow them, and the
symptom would be an app whose every request returns index.html.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import _serve_built_frontend, app


def test_the_api_still_works_with_the_ui_mounted():
    """
    THE test. A StaticFiles mount at "/" matches every path, so it has to be
    added after api_router. If that order is ever reversed, /health returns
    the HTML page instead of JSON and nothing else in the app works.
    """
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_the_tool_endpoints_are_not_swallowed_either():
    client = TestClient(app)

    body = client.get("/tools").json()

    assert "tools" in body
    assert any(tool["name"] == "read_file" for tool in body["tools"])


def test_a_missing_build_is_not_an_error(tmp_path, monkeypatch):
    """
    Normal in development, where Vite serves the UI on its own port. The
    backend should log and carry on, not refuse to start.
    """
    import app.main as main

    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    blank = FastAPI()
    monkeypatch.setattr(main, "app", blank)

    _serve_built_frontend()  # must not raise

    assert not any(getattr(route, "name", "") == "ui" for route in blank.routes)
