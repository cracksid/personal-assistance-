"""
The plugin SDK -- everything a plugin author imports, in one place.

    from app.plugins.sdk import BaseModel, Field, Tool, ToolContext, ToolResult

WHY THIS FILE EXISTS AT ALL, GIVEN IT ONLY RE-EXPORTS.

A plugin author could import Tool from app.tools.base and ToolResult from
the same place and BaseModel from pydantic. Nothing stops them. But then
every plugin ever written would be coupled to JARVIS's internal layout, and
moving app/tools/base.py -- an ordinary refactor -- would break code living
outside this repository that nobody here can fix.

This module is the promise: these names, at this path, keep working. What
sits behind them can move. That is the whole job of a public API, and it is
worth a file that contains almost no code.

WHAT A PLUGIN CANNOT DO, BY CONSTRUCTION.

Notice what is missing below. There is no way to prompt the user, no way to
write an audit row, no way to bypass confirmation, and no way to reach the
gate. A plugin declares requires_confirmation and describes its action; the
core decides what happens. That is the same deal every built-in tool gets
-- CLAUDE.md's "same interface, no second system" -- and it is why a plugin
is not a privileged thing.

WHAT A PLUGIN CAN DO, HONESTLY STATED.

A plugin is ordinary Python running in this process, with this process's
permissions. It can open sockets, read files outside the sandbox, and
import anything installed. There is no sandbox around plugin code and this
file does not pretend otherwise.

What the gate guarantees is narrower and still worth having: nothing a
plugin declares as a Tool can run without an audit row, and nothing it
declares destructive can run without a human "yes". Install plugins you
trust, the same way you would treat any Python package.
"""

from pydantic import BaseModel, Field

from app.tools.base import Tool, ToolContext, ToolError, ToolResult

# __all__ is the export list: it says which names `from ... import *` takes,
# and more usefully it documents intent -- these are the supported ones.
__all__ = [
    "BaseModel",
    "Field",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolResult",
]
