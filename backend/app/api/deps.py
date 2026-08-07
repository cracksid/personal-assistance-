"""
Shared FastAPI dependencies for the API layer.

Keeping these in one module means routes declare *what* they need and this
file decides *how* it gets built -- and tests can swap any of it out with
app.dependency_overrides without touching route code.
"""

from app.core.agent import Agent
from app.memory.store import MemoryStore
from app.providers.factory import (
    get_llm_provider,
    get_stt_provider as build_stt_provider,
    get_tts_provider as build_tts_provider,
)
from app.providers.speech import STTProvider, TTSProvider

# Built once and reused. Chroma opens files on disk and loads an embedding
# model into memory, so constructing a new store for every WebSocket
# connection would be slow and wasteful. `None` until first use, so importing
# this module never touches the disk -- which keeps tests that don't need
# memory fast.
_memory_store: MemoryStore | None = None


def get_memory_store() -> MemoryStore:
    """Return the shared memory store, creating it on first use."""
    # `global` lets this function reassign the module-level name above rather
    # than creating a new local variable that vanishes when it returns.
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore()
    return _memory_store


def get_agent() -> Agent:
    """
    Build the agent for a connection.

    Note this file -- in the API layer -- is where the provider gets chosen,
    via the factory. app/core/agent.py receives the finished provider and
    never learns which one it is.
    """
    return Agent(get_llm_provider(), get_memory_store())


# Speech engines are cached for the same reason as the memory store, only
# more so: each holds hundreds of megabytes of model weights in memory.
# Rebuilding one per request would reload them every time.
_stt_provider: STTProvider | None = None
_tts_provider: TTSProvider | None = None


def get_stt() -> STTProvider:
    """Return the shared speech-to-text engine, creating it on first use."""
    global _stt_provider
    if _stt_provider is None:
        _stt_provider = build_stt_provider()
    return _stt_provider


def get_tts() -> TTSProvider:
    """Return the shared text-to-speech engine, creating it on first use."""
    global _tts_provider
    if _tts_provider is None:
        _tts_provider = build_tts_provider()
    return _tts_provider
