"""
Entry point for the FastAPI application.

Run with:
    uvicorn app.main:app --reload

`app` below is the FastAPI application object -- Uvicorn imports this exact
name (app.main:app means "the `app` variable inside app/main.py") and uses
it to handle incoming HTTP requests.
"""

from fastapi import FastAPI

from app.config import settings

app = FastAPI(title=settings.app_name)


@app.get("/health")
def health_check() -> dict[str, str]:
    """
    Returns 200 OK with a small JSON body if the server is running.
    Used later (Docker, deployment, monitoring) to check "is JARVIS alive?"
    without needing to know anything about what JARVIS actually does.
    """
    return {"status": "ok", "environment": settings.environment}
