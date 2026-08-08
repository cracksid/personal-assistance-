"""
Tests for the confirmation gate.

The gate is the single choke point every tool execution passes through, so
these tests are about the guarantees it makes rather than about any
particular tool:

  - a destructive tool does NOT run until a human approves
  - the audit row is written BEFORE the tool runs, not after
  - an approval cannot be replayed, and expires
  - what gets approved is exactly what runs

A fake tool is used throughout so the tests describe gate behaviour, not
filesystem behaviour.
"""

from datetime import timedelta

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.core.gate import ConfirmationRequired, ToolGate
from app.db.models import AuditLog
from app.tools import registry
from app.tools.base import Tool, ToolError, ToolResult


class Args(BaseModel):
    target: str


class SafeTool(Tool):
    name = "safe_thing"
    description = "Does something harmless."
    input_schema = Args
    requires_confirmation = False

    def __init__(self) -> None:
        self.ran_with: list[str] = []

    def describe_action(self, args: Args) -> str:
        return f"Look at {args.target}"

    async def run(self, args: Args) -> ToolResult:
        self.ran_with.append(args.target)
        return ToolResult(output=f"looked at {args.target}")


class DangerousTool(SafeTool):
    name = "dangerous_thing"
    description = "Destroys something."
    requires_confirmation = True

    def describe_action(self, args: Args) -> str:
        return f"PERMANENTLY DELETE {args.target}"


class ExplodingTool(SafeTool):
    name = "exploding_thing"

    async def run(self, args: Args) -> ToolResult:
        raise ToolError("that path is outside the sandbox")


@pytest.fixture
def tools() -> dict[str, Tool]:
    """Swap the real registry for fakes, and restore it afterwards."""
    registry.reset()
    made = {
        "safe": SafeTool(),
        "dangerous": DangerousTool(),
        "exploding": ExplodingTool(),
    }
    for tool in made.values():
        registry.register(tool)
    yield made
    registry.reset()
    registry.load_builtin_tools()


@pytest.fixture
def gate() -> ToolGate:
    return ToolGate()


def audit_rows(db) -> list[AuditLog]:
    return list(db.scalars(select(AuditLog).order_by(AuditLog.id)))


# --- the core guarantee ----------------------------------------------------


@pytest.mark.anyio
async def test_a_safe_tool_just_runs(db_session, gate: ToolGate, tools):
    result = await gate.invoke(db_session, "safe_thing", {"target": "a file"})

    assert isinstance(result, ToolResult)
    assert result.ok
    assert tools["safe"].ran_with == ["a file"]


@pytest.mark.anyio
async def test_a_dangerous_tool_does_not_run_until_confirmed(
    db_session, gate: ToolGate, tools
):
    """
    THE test for this phase. The call returns a description to approve, and
    the tool has not executed.
    """
    outcome = await gate.invoke(db_session, "dangerous_thing", {"target": "notes.txt"})

    assert isinstance(outcome, ConfirmationRequired)
    assert outcome.description == "PERMANENTLY DELETE notes.txt"
    assert tools["dangerous"].ran_with == []  # nothing happened


@pytest.mark.anyio
async def test_confirming_runs_it(db_session, gate: ToolGate, tools):
    pending = await gate.invoke(db_session, "dangerous_thing", {"target": "notes.txt"})

    result = await gate.confirm(db_session, pending.confirmation_id)

    assert result.ok
    assert tools["dangerous"].ran_with == ["notes.txt"]


@pytest.mark.anyio
async def test_cancelling_means_it_never_runs(db_session, gate: ToolGate, tools):
    pending = await gate.invoke(db_session, "dangerous_thing", {"target": "notes.txt"})

    assert gate.cancel(pending.confirmation_id) is True

    result = await gate.confirm(db_session, pending.confirmation_id)
    assert result.ok is False
    assert tools["dangerous"].ran_with == []


@pytest.mark.anyio
async def test_a_confirmation_cannot_be_replayed(db_session, gate: ToolGate, tools):
    """
    One approval, one execution. Otherwise a replayed id could delete a
    second file the user never saw described.
    """
    pending = await gate.invoke(db_session, "dangerous_thing", {"target": "notes.txt"})
    await gate.confirm(db_session, pending.confirmation_id)

    again = await gate.confirm(db_session, pending.confirmation_id)

    assert again.ok is False
    assert tools["dangerous"].ran_with == ["notes.txt"]  # still just once


@pytest.mark.anyio
async def test_an_expired_confirmation_is_refused(db_session, gate: ToolGate, tools):
    """
    A "yes" given now must not be redeemable much later against a request
    the user has forgotten agreeing to.
    """
    pending = await gate.invoke(db_session, "dangerous_thing", {"target": "notes.txt"})
    # Reach in and age it, rather than making the test sleep for minutes.
    gate._pending[pending.confirmation_id].expires_at -= timedelta(hours=1)

    result = await gate.confirm(db_session, pending.confirmation_id)

    assert result.ok is False
    assert "expired" in result.error
    assert tools["dangerous"].ran_with == []


@pytest.mark.anyio
async def test_an_unknown_confirmation_id_is_refused(db_session, gate: ToolGate, tools):
    result = await gate.confirm(db_session, "not-a-real-id")

    assert result.ok is False


# --- the audit log ---------------------------------------------------------


@pytest.mark.anyio
async def test_the_audit_row_is_written_before_the_tool_runs(
    db_session, gate: ToolGate, tools
):
    """
    CLAUDE.md requires the row to exist BEFORE execution, so a crash
    mid-tool still leaves evidence. This proves it by looking at the audit
    table from inside the tool itself -- at that moment the row must
    already be there, and still say "started".
    """
    seen: list[tuple[str, str]] = []

    class Watcher(SafeTool):
        name = "watcher"

        async def run(self, args: Args) -> ToolResult:
            rows = audit_rows(db_session)
            seen.append((rows[-1].tool_name, rows[-1].status))
            return ToolResult(output="ok")

    registry.register(Watcher())
    await gate.invoke(db_session, "watcher", {"target": "x"})

    assert seen == [("watcher", "started")]


@pytest.mark.anyio
async def test_a_successful_run_is_recorded(db_session, gate: ToolGate, tools):
    await gate.invoke(db_session, "safe_thing", {"target": "a file"})

    row = audit_rows(db_session)[-1]
    assert row.tool_name == "safe_thing"
    assert row.status == "success"
    assert row.requires_confirmation is False
    assert row.confirmed is False
    assert "a file" in row.arguments


@pytest.mark.anyio
async def test_the_log_records_that_a_dangerous_tool_was_approved(
    db_session, gate: ToolGate, tools
):
    """
    So the log can answer "did anything destructive run without approval?"
    """
    pending = await gate.invoke(db_session, "dangerous_thing", {"target": "notes.txt"})
    await gate.confirm(db_session, pending.confirmation_id)

    row = audit_rows(db_session)[-1]
    assert row.requires_confirmation is True
    assert row.confirmed is True
    assert row.status == "success"


@pytest.mark.anyio
async def test_nothing_is_logged_for_a_call_still_awaiting_approval(
    db_session, gate: ToolGate, tools
):
    """A request that has not run is not an execution."""
    await gate.invoke(db_session, "dangerous_thing", {"target": "notes.txt"})

    assert audit_rows(db_session) == []


@pytest.mark.anyio
async def test_a_failing_tool_is_recorded_as_an_error(db_session, gate: ToolGate, tools):
    result = await gate.invoke(db_session, "exploding_thing", {"target": "x"})

    assert result.ok is False
    row = audit_rows(db_session)[-1]
    assert row.status == "error"
    assert "outside the sandbox" in row.error_message


# --- argument validation ---------------------------------------------------


@pytest.mark.anyio
async def test_bad_arguments_are_rejected_before_anything_runs(
    db_session, gate: ToolGate, tools
):
    result = await gate.invoke(db_session, "dangerous_thing", {"wrong_field": 1})

    assert result.ok is False
    assert tools["dangerous"].ran_with == []
    # Recorded, because repeated malformed calls to a destructive tool are
    # worth being able to see.
    assert audit_rows(db_session)[-1].status == "invalid_arguments"


@pytest.mark.anyio
async def test_an_unknown_tool_is_refused(db_session, gate: ToolGate, tools):
    result = await gate.invoke(db_session, "rm_minus_rf", {})

    assert result.ok is False
    assert "No tool named" in result.error


# --- the registry ----------------------------------------------------------


def test_a_tool_cannot_shadow_an_existing_one(tools):
    """
    Once plugins exist, a plugin named "delete_file" must not be able to
    silently replace the built-in that asks for confirmation.
    """
    with pytest.raises(ValueError, match="already registered"):
        registry.register(SafeTool())
