"""
The Tool interface.

CLAUDE.md: "Every tool -- built-in or plugin -- implements the same Tool
interface: name, description, Pydantic input schema, run(),
requires_confirmation, and describe_action()."

The important part is what is NOT here. A tool cannot prompt the user, cannot
write to the audit log, and cannot decide whether it is dangerous. It only
*declares* whether it needs confirmation and describes what it is about to
do. Everything else is the gate's job, in core/.

That split is the whole point. If each tool implemented its own "are you
sure?", then tool number 40 -- written months from now, possibly by a plugin
author -- would eventually forget, and the failure would be silent and
destructive. A tool that forgets to set requires_confirmation is merely
ungated; a tool that forgets to *ask* would be unstoppable.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    # Imported for type checking only, so this module has no runtime
    # dependency on the database or memory layers -- a tool that needs
    # neither should not drag them in.
    from sqlalchemy.orm import Session

    from app.memory.store import MemoryStore


class ToolError(Exception):
    """Raised when a tool cannot do what was asked."""


@dataclass
class ToolContext:
    """
    Everything a tool might need beyond its own arguments.

    Passed in by the gate rather than reached for, so a tool never opens its
    own database session or decides which user it is acting for. Most tools
    ignore it entirely -- the filesystem tools use none of it.

    Added when there were still only five tools. Retrofitting a context
    parameter across forty of them would have been far more painful.
    """

    db: "Session"
    user_id: int | None = None
    memory: "MemoryStore | None" = None


class ToolResult(BaseModel):
    """
    What a tool hands back.

    Deliberately not raw Python objects: the output eventually goes into a
    prompt, so it has to be text. `ok` lets the caller distinguish "the tool
    ran and the answer is no" from "the tool failed".
    """

    ok: bool = True
    output: str = ""
    error: str | None = None


class Tool(ABC):
    """Base class for every capability JARVIS can invoke."""

    # Identifier the model uses to call it. Lowercase with underscores.
    name: str

    # Shown to the model. This is how it decides when to use the tool, so
    # it should say when to use it, not just what it does.
    description: str

    # Pydantic model describing the arguments. Pydantic validates and
    # coerces the model's JSON before any tool code runs, so a tool never
    # has to defend against a missing or mistyped field.
    input_schema: type[BaseModel]

    # Whether the gate must get a human "yes" first. Default False, so a
    # tool is only dangerous when it says so -- but see describe_action:
    # the author still has to state plainly what will happen.
    requires_confirmation: bool = False

    @abstractmethod
    def describe_action(self, args: BaseModel) -> str:
        """
        One plain sentence describing exactly what running this will do.

        This is what the user reads before approving. It must be concrete --
        "Delete C:\\Users\\Admin\\notes.txt (2.4 KB)", not "delete a file" --
        because a vague description makes approval meaningless.
        """
        raise NotImplementedError

    @abstractmethod
    async def run(self, args: BaseModel, context: ToolContext) -> ToolResult:
        """
        Do the thing.

        Async for every tool, even ones that only touch the local disk, so
        that built-in tools and future network tools (Phase 10) share one
        signature. Blocking work inside goes on a worker thread.

        `context` carries the database session, the acting user, and the
        memory store. Most tools ignore it; the ones that need it get it
        handed over rather than reaching for globals.

        By the time this is called the gate has already validated the
        arguments, written an audit row, and obtained confirmation if it was
        required. A tool must NOT re-check any of that.
        """
        raise NotImplementedError
