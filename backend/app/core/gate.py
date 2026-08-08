"""
THE CONFIRMATION GATE.

CLAUDE.md: "The confirmation gate is exactly one choke point in core/.
Individual tools must never implement their own 'are you sure?' prompt. A
tool declares requires_confirmation and a describe_action() method; the core
decides. This is so a forgotten check in tool number 40 can't delete a
folder."

Every tool execution in JARVIS goes through ToolGate.invoke(). There is no
other path -- the API layer holds no direct reference to a tool's run().

What the gate does, in order:

    1. Look the tool up. Unknown name -> refuse.
    2. Validate arguments against the tool's Pydantic schema.
    3. If the tool requires confirmation and none was given, STOP and return
       a description for a human to approve. Nothing has run.
    4. Write an audit row with status="started" AND COMMIT IT.
    5. Run the tool.
    6. Update the row to "success" or "error".

Step 4 committing before step 5 is deliberate, not an accident of ordering.
If the process is killed mid-execution, the log still shows a "started" row
naming the tool and its arguments. Writing the row afterwards would mean a
crash during the destructive part left no evidence it ever happened.
"""

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import AuditLog
from app.tools import registry
from app.tools.base import Tool, ToolError, ToolResult

logger = logging.getLogger(__name__)


class ConfirmationRequired(BaseModel):
    """
    Returned instead of a result when a human has to approve first.

    Note what it does NOT contain: the tool's arguments. Those stay
    server-side against the confirmation id, so what gets approved is
    exactly what runs. If the caller handed the arguments back with the
    confirmation, a client could approve one thing and execute another.
    """

    confirmation_id: str
    tool_name: str
    description: str
    expires_at: datetime


@dataclass
class _Pending:
    """A validated call waiting for a human decision."""

    tool: Tool
    args: BaseModel
    user_id: int | None
    expires_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ToolGate:
    """The only way to run a tool."""

    def __init__(self) -> None:
        self._pending: dict[str, _Pending] = {}

    async def invoke(
        self,
        db: Session,
        tool_name: str,
        raw_args: dict,
        user_id: int | None = None,
    ) -> ToolResult | ConfirmationRequired:
        """
        Run a tool, or ask for approval first.

        Returns ConfirmationRequired if the tool is destructive and has not
        been approved. In that case NOTHING has run and nothing has changed.
        """
        self._drop_expired()

        tool = registry.get_tool(tool_name)
        if tool is None:
            known = ", ".join(t.name for t in registry.all_tools())
            return ToolResult(
                ok=False, error=f"No tool named {tool_name!r}. Available: {known}."
            )

        try:
            args = tool.input_schema(**raw_args)
        except ValidationError as exc:
            # Recorded rather than silently rejected: repeated malformed
            # calls to a destructive tool are worth being able to see.
            await self._audit_rejected(db, tool, raw_args, user_id, str(exc))
            return ToolResult(
                ok=False, error=f"Invalid arguments for {tool_name}: {exc.errors()}"
            )

        if tool.requires_confirmation:
            return self._request_confirmation(tool, args, user_id)

        return await self._execute(db, tool, args, user_id, confirmed=False)

    async def confirm(
        self, db: Session, confirmation_id: str, user_id: int | None = None
    ) -> ToolResult:
        """Approve a pending call and run it."""
        self._drop_expired()

        pending = self._pending.pop(confirmation_id, None)
        if pending is None:
            return ToolResult(
                ok=False,
                error="That confirmation is unknown or has expired. Ask again.",
            )

        return await self._execute(
            db, pending.tool, pending.args, pending.user_id, confirmed=True
        )

    def cancel(self, confirmation_id: str) -> bool:
        """Discard a pending call. Returns whether there was one."""
        return self._pending.pop(confirmation_id, None) is not None

    def pending_count(self) -> int:
        self._drop_expired()
        return len(self._pending)

    # --- internals ---------------------------------------------------------

    def _request_confirmation(
        self, tool: Tool, args: BaseModel, user_id: int | None
    ) -> ConfirmationRequired:
        confirmation_id = secrets.token_urlsafe(16)
        expires_at = _now() + timedelta(seconds=settings.tool_confirmation_ttl_seconds)

        self._pending[confirmation_id] = _Pending(
            tool=tool, args=args, user_id=user_id, expires_at=expires_at
        )

        description = tool.describe_action(args)
        logger.info("Awaiting confirmation for %s: %s", tool.name, description)

        return ConfirmationRequired(
            confirmation_id=confirmation_id,
            tool_name=tool.name,
            description=description,
            expires_at=expires_at,
        )

    async def _execute(
        self,
        db: Session,
        tool: Tool,
        args: BaseModel,
        user_id: int | None,
        confirmed: bool,
    ) -> ToolResult:
        """Audit, then run, then record the outcome."""
        entry = await asyncio.to_thread(
            self._write_audit_row, db, tool, args, user_id, confirmed
        )

        logger.info("Running %s (audit id=%s)", tool.name, entry.id)

        try:
            result = await tool.run(args)
        except ToolError as exc:
            # An expected refusal -- outside the sandbox, missing file. The
            # message is written for a human and is safe to show.
            await self._finish_audit(db, entry, "error", str(exc))
            return ToolResult(ok=False, error=str(exc))
        except Exception as exc:
            # A bug. Log the traceback, tell the caller something generic.
            logger.error("Tool %s raised unexpectedly", tool.name, exc_info=True)
            await self._finish_audit(db, entry, "error", repr(exc))
            return ToolResult(ok=False, error=f"{tool.name} failed unexpectedly.")

        await self._finish_audit(
            db, entry, "success" if result.ok else "error", result.error
        )
        return result

    def _write_audit_row(
        self,
        db: Session,
        tool: Tool,
        args: BaseModel,
        user_id: int | None,
        confirmed: bool,
    ) -> AuditLog:
        entry = AuditLog(
            user_id=user_id,
            tool_name=tool.name,
            arguments=json.dumps(args.model_dump(), default=str)[:4000],
            requires_confirmation=tool.requires_confirmation,
            confirmed=confirmed,
            status="started",
        )
        db.add(entry)
        # Committed here, before the tool runs, so a crash mid-execution
        # still leaves a "started" row naming what was attempted.
        db.commit()
        return entry

    async def _finish_audit(
        self, db: Session, entry: AuditLog, status: str, error: str | None
    ) -> None:
        def update() -> None:
            entry.status = status
            entry.error_message = error[:2000] if error else None
            db.commit()

        await asyncio.to_thread(update)

    async def _audit_rejected(
        self,
        db: Session,
        tool: Tool,
        raw_args: dict,
        user_id: int | None,
        error: str,
    ) -> None:
        def write() -> None:
            db.add(
                AuditLog(
                    user_id=user_id,
                    tool_name=tool.name,
                    arguments=json.dumps(raw_args, default=str)[:4000],
                    requires_confirmation=tool.requires_confirmation,
                    confirmed=False,
                    status="invalid_arguments",
                    error_message=error[:2000],
                )
            )
            db.commit()

        await asyncio.to_thread(write)

    def _drop_expired(self) -> None:
        """
        Forget approvals nobody acted on.

        Without this, a "yes" given now could be redeemed an hour later
        against a request the user has long forgotten agreeing to.
        """
        now = _now()
        for key in [k for k, v in self._pending.items() if v.expires_at <= now]:
            logger.info("Confirmation %s expired unused", key)
            del self._pending[key]
