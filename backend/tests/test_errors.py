"""
Tests for the global exception handlers in app/errors.py.

These build a tiny throwaway FastAPI app (not the real app.main one) with
routes that deliberately raise errors, just to verify
register_exception_handlers turns those into the right JSON responses --
without adding test-only routes to the real app.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors import AppError, register_exception_handlers

error_demo_app = FastAPI()
register_exception_handlers(error_demo_app)


@error_demo_app.get("/boom-app-error")
def boom_app_error():
    raise AppError("nope", status_code=404)


@error_demo_app.get("/boom-unexpected")
def boom_unexpected():
    raise ValueError("surprise")


client = TestClient(error_demo_app, raise_server_exceptions=False)


def test_app_error_returns_declared_status_and_message():
    response = client.get("/boom-app-error")

    assert response.status_code == 404
    assert response.json() == {"error": "nope"}


def test_unexpected_error_returns_generic_500_without_leaking_details():
    response = client.get("/boom-unexpected")

    assert response.status_code == 500
    assert response.json() == {"error": "Internal server error"}
    assert "surprise" not in response.text
