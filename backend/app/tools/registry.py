"""
The tool registry.

One dictionary of every tool JARVIS can run, keyed by name. Built once at
import and read everywhere else.

CLAUDE.md: "Plugins are loaded from a folder instead of imported. Same
interface, no second system." Phase 12 will add plugin discovery, and it
will register into this same dictionary -- a plugin is not a different kind
of thing, just a Tool that arrived from somewhere else.
"""

import logging

from app.tools.base import Tool
from app.tools.builtin import build_builtin_tools
from app.tools.filesystem import build_filesystem_tools
from app.tools.web import build_web_tools

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> None:
    """
    Add a tool, refusing to shadow an existing one.

    A silent overwrite would be a real hazard once plugins exist: a plugin
    named "delete_file" could quietly replace the built-in that asks for
    confirmation with one that does not.
    """
    if tool.name in _REGISTRY:
        raise ValueError(
            f"A tool named {tool.name!r} is already registered. Names must be unique."
        )
    _REGISTRY[tool.name] = tool


def get_tool(name: str) -> Tool | None:
    """Look up a tool by name, or None if there is no such tool."""
    return _REGISTRY.get(name)


def all_tools() -> list[Tool]:
    """Every registered tool, in a stable order."""
    return [_REGISTRY[name] for name in sorted(_REGISTRY)]


def reset() -> None:
    """Empty the registry. For tests only."""
    _REGISTRY.clear()


def load_builtin_tools() -> None:
    """Register the tools that ship with JARVIS."""
    for tool in [
        *build_filesystem_tools(),
        *build_builtin_tools(),
        *build_web_tools(),
    ]:
        register(tool)
    logger.info("Registered %s built-in tool(s)", len(_REGISTRY))


# Populated at import so anything that imports the registry sees a working
# tool set, without every caller having to remember to initialise it.
if not _REGISTRY:
    load_builtin_tools()
