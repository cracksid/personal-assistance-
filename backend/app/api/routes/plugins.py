"""
REST endpoint for seeing what plugins did or did not load.

    GET /plugins   -> every plugin file found, and what happened to it

This exists because the first question a plugin author asks is "why isn't
my plugin loading?", and the honest answer is usually a typo, a missing
register(), or a tool name that is already taken. Making them read a
terminal to find that out is a bad experience; the loader already records
the reason, so this hands it back.

There is deliberately no endpoint to install, enable, reload or delete a
plugin. A plugin is arbitrary Python running with this process's
permissions, so adding it is a decision made by putting a file in a folder
-- something the person at the keyboard does, not something reachable over
HTTP. An "install plugin" endpoint on a server bound to localhost is still
one prompt-injected fetch away from being a remote code execution feature.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.plugins import loader

router = APIRouter(prefix="/plugins", tags=["plugins"])


class PluginInfo(BaseModel):
    name: str
    path: str
    loaded: bool
    tools: list[str]
    error: str | None = None


class PluginListing(BaseModel):
    plugins_dir: str
    enabled: bool
    plugins: list[PluginInfo]


@router.get("", response_model=PluginListing)
def list_plugins() -> PluginListing:
    """Show every plugin file found at startup, loaded or not."""
    from app.config import settings

    return PluginListing(
        plugins_dir=str(loader.plugins_dir()),
        enabled=settings.plugins_enabled,
        plugins=[
            PluginInfo(
                name=r.name,
                path=r.path,
                loaded=r.loaded,
                tools=r.tools,
                error=r.error,
            )
            for r in loader.reports()
        ],
    )
