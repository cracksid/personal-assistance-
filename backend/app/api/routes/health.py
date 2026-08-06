"""Health check route -- confirms the server is up and responding."""

from fastapi import APIRouter

from app.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check() -> dict[str, str]:
    """
    Returns 200 OK with a small JSON body if the server is running.
    Used later (Docker, deployment, monitoring) to check "is JARVIS alive?"
    without needing to know anything about what JARVIS actually does.
    """
    return {"status": "ok", "environment": settings.environment}
