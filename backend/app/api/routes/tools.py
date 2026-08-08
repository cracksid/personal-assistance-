"""
REST endpoints for listing and running tools.

    GET  /tools                     -> what JARVIS can do
    POST /tools/{name}/invoke       -> run it, or get a confirmation request
    POST /tools/confirm             -> approve a pending destructive call
    POST /tools/cancel              -> decline one

Everything routes through ToolGate. This module never touches a tool's
run() directly -- if it did, there would be two paths to execution and only
one of them gated, which is exactly the failure CLAUDE.md's "exactly one
choke point" rule exists to prevent.
"""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_gate
from app.core.gate import ConfirmationRequired, ToolGate
from app.db import crud
from app.db.session import get_db
from app.tools import registry
from app.tools.base import ToolResult
from app.tools.paths import sandbox_root

router = APIRouter(prefix="/tools", tags=["tools"])
logger = logging.getLogger(__name__)


class ToolInfo(BaseModel):
    name: str
    description: str
    requires_confirmation: bool
    input_schema: dict


class ToolListing(BaseModel):
    sandbox_root: str
    tools: list[ToolInfo]


class ConfirmRequest(BaseModel):
    confirmation_id: str


class InvokeResponse(BaseModel):
    """
    One of two shapes, never both.

    `result` is set when the tool ran. `confirmation` is set when it needs
    approval first and nothing has happened yet. Keeping them as separate
    fields rather than a union makes the caller check which one they got.
    """

    result: ToolResult | None = None
    confirmation: ConfirmationRequired | None = None


@router.get("")
async def list_tools() -> ToolListing:
    """Everything JARVIS can do, and where it is allowed to do it."""
    return ToolListing(
        sandbox_root=str(sandbox_root()),
        tools=[
            ToolInfo(
                name=tool.name,
                description=tool.description,
                requires_confirmation=tool.requires_confirmation,
                input_schema=tool.input_schema.model_json_schema(),
            )
            for tool in registry.all_tools()
        ],
    )


@router.post("/{name}/invoke")
async def invoke(
    name: str,
    arguments: dict,
    db: Session = Depends(get_db),
    gate: ToolGate = Depends(get_gate),
) -> InvokeResponse:
    """
    Run a tool.

    A destructive tool returns a `confirmation` instead of a `result`, and
    has NOT run. Approve it at /tools/confirm to actually execute.
    """
    owner = crud.get_or_create_owner(db)
    outcome = await gate.invoke(db, name, arguments, user_id=owner.id)

    if isinstance(outcome, ConfirmationRequired):
        return InvokeResponse(confirmation=outcome)
    return InvokeResponse(result=outcome)


@router.post("/confirm")
async def confirm(
    body: ConfirmRequest,
    db: Session = Depends(get_db),
    gate: ToolGate = Depends(get_gate),
) -> ToolResult:
    """Approve a pending destructive call and run it."""
    owner = crud.get_or_create_owner(db)
    return await gate.confirm(db, body.confirmation_id, user_id=owner.id)


@router.post("/cancel")
async def cancel(
    body: ConfirmRequest, gate: ToolGate = Depends(get_gate)
) -> ToolResult:
    """Decline a pending call and forget it."""
    if gate.cancel(body.confirmation_id):
        return ToolResult(output="Cancelled.")
    return ToolResult(ok=False, error="No such pending confirmation.")
