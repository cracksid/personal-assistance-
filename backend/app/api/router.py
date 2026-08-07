"""
Combines every route module into one router that main.py includes once.

Each future phase that adds routes (files, memory, automation, ...) gets
its own file in api/routes/ and one line added here -- main.py itself
never needs to change just because a new route was added.
"""

from fastapi import APIRouter

from app.api.routes import chat, health, vision, voice

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(chat.router)
api_router.include_router(voice.router)
api_router.include_router(vision.router)
