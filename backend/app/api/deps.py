"""
Shared FastAPI dependencies for the API layer.

Keeping these in one module means routes declare *what* they need and this
file decides *how* it gets built -- and tests can swap any of it out with
app.dependency_overrides without touching route code.
"""

from app.core.agent import Agent
from app.providers.factory import get_llm_provider


def get_agent() -> Agent:
    """
    Build the agent for a request.

    Note this file -- in the API layer -- is where the provider gets chosen,
    via the factory. app/core/agent.py receives the finished provider and
    never learns which one it is.
    """
    return Agent(get_llm_provider())
