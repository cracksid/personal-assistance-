"""
Small built-in tools that are not about the filesystem.

Both are borrowed in spirit from other JARVIS projects, but built as Tools
rather than as bespoke feature modules. That is the whole benefit of having
one interface: "remember this" arrives with the confirmation gate, the audit
log, and schema validation already attached, because it cannot arrive any
other way.
"""

import asyncio
import logging
from datetime import datetime

from pydantic import BaseModel, Field

from app.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


class NoArgs(BaseModel):
    """A tool that takes no input still needs a schema to describe that."""


class CurrentTime(Tool):
    name = "get_current_time"
    description = (
        "Get the current local date and time. Use this whenever the answer "
        "depends on what day or time it is now -- you have no other way to "
        "know, and guessing will be wrong."
    )
    input_schema = NoArgs
    requires_confirmation = False

    def describe_action(self, args: NoArgs) -> str:
        return "Check the current date and time"

    async def run(self, args: NoArgs, context: ToolContext) -> ToolResult:
        # Local time, not UTC: the user asking "what time is it" means where
        # they are sitting. Stored data stays UTC (see db/models.py); this is
        # display, which is a different concern.
        now = datetime.now().astimezone()
        return ToolResult(output=now.strftime("%A, %d %B %Y, %H:%M %Z"))


class RememberInput(BaseModel):
    content: str = Field(
        description=(
            "The fact, written as a complete sentence about the user, "
            "e.g. 'The user's sister is called Priya'."
        )
    )
    kind: str = Field(
        default="other",
        description="One of: identity, preference, project, other.",
    )


class RememberFact(Tool):
    """
    Lets the user say "remember that..." and have it stick.

    Phase 6 already extracts facts automatically after every exchange, but
    that is a guess made by a model. This is the explicit channel: when
    someone says "remember X", it should be remembered, not left to an
    extractor that measurably misses things two runs out of three on a
    small local model.
    """

    name = "remember_fact"
    description = (
        "Store something about the user permanently, so it is available in "
        "future conversations. Use this when the user explicitly asks you to "
        "remember something. Do NOT use it for passing details you merely "
        "noticed -- those are captured automatically."
    )
    input_schema = RememberInput

    # Storing a fact is additive and reversible, so no confirmation. It is
    # still audited, like everything else that runs through the gate.
    requires_confirmation = False

    def describe_action(self, args: RememberInput) -> str:
        return f"Remember: {args.content}"

    async def run(self, args: RememberInput, context: ToolContext) -> ToolResult:
        if context.memory is None or context.user_id is None:
            return ToolResult(
                ok=False, error="Memory is not available in this context."
            )

        # to_thread because MemoryStore is synchronous -- it writes to SQLite
        # and computes an embedding, both of which would block the event loop.
        stored = await asyncio.to_thread(
            context.memory.remember,
            context.db,
            context.user_id,
            args.content,
            args.kind,
            None,
        )

        if stored is None:
            # remember() returns None when the fact is already known. That is
            # a success from the user's point of view, not a failure.
            return ToolResult(output=f"Already knew that: {args.content}")
        return ToolResult(output=f"Remembered: {args.content}")


def build_builtin_tools() -> list[Tool]:
    return [CurrentTime(), RememberFact()]
