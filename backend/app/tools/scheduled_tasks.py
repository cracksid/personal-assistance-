"""
Tools for creating and managing scheduled tasks.

A scheduled task is a prompt JARVIS runs to itself on a repeat -- "every
morning at 8, summarise my pending reminders". The model creates them the
same way it creates a reminder, through a tool.

WHY create_scheduled_task ASKS FOR CONFIRMATION AND create_reminder DOES NOT.

A reminder is one line of text delivered once. A task is a standing
instruction that runs the agent loop, with tools, repeatedly, forever, each
run costing a model call. Setting one up is the only moment a human is
guaranteed to be present, so it is the right moment to ask -- and the
description spells out the schedule and the prompt so what is being agreed
to is visible.

That is a judgement call rather than a rule: nothing here deletes anything.
The reasoning is that "recurring" and "costs money on a timer" together are
enough to want a human to have seen it once.
"""

import asyncio
import logging
from datetime import datetime, timezone

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.automation import schedules
from app.config import settings
from app.db.models import ScheduledTask
from app.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


def _format_local(when_utc: datetime) -> str:
    """Render a stored naive-UTC instant back in the user's local time."""
    aware = when_utc.replace(tzinfo=timezone.utc)
    return aware.astimezone().strftime("%A, %d %B at %H:%M")


class CreateTaskInput(BaseModel):
    name: str = Field(
        description="A short handle for the task, e.g. 'morning briefing'."
    )
    prompt: str = Field(
        description=(
            "The instruction to run each time, written as if the user were "
            "asking it, e.g. 'Summarise my pending reminders for today.' "
            "It must be self-contained -- the task cannot ask a follow-up "
            "question, and destructive tools will be refused when it runs."
        )
    )
    schedule_kind: str = Field(
        default="daily",
        description="'daily' for a wall-clock time, 'interval' for every N seconds.",
    )
    daily_at: str | None = Field(
        default=None,
        description="For schedule_kind='daily': 24-hour local time, e.g. '08:00'.",
    )
    interval_seconds: int | None = Field(
        default=None,
        description=(
            "For schedule_kind='interval': how often to repeat. 3600 is "
            "hourly. Anything under the configured minimum is refused."
        ),
    )


class CreateScheduledTask(Tool):
    name = "create_scheduled_task"
    description = (
        "Set up something for JARVIS to do on a repeating schedule -- a daily "
        "briefing, an hourly check. Use this for anything recurring. For a "
        "one-off nudge at a single moment, use create_reminder instead."
    )
    input_schema = CreateTaskInput

    # See the module docstring: recurring, and each run costs a model call.
    requires_confirmation = True

    def describe_action(self, args: CreateTaskInput) -> str:
        when = schedules.describe(
            args.schedule_kind, args.interval_seconds, args.daily_at
        )
        return (
            f"Set up a repeating task {when!r} called {args.name!r}, "
            f"which will run: {args.prompt}"
        )

    async def run(self, args: CreateTaskInput, context: ToolContext) -> ToolResult:
        if context.user_id is None:
            return ToolResult(ok=False, error="No user to schedule a task for.")

        try:
            schedules.validate(
                args.schedule_kind, args.interval_seconds, args.daily_at
            )
        except schedules.ScheduleError as exc:
            # Returned rather than raised: the model reads this and can fix
            # its own call, which a ToolError message also allows but this
            # keeps the failure clearly a refusal rather than a fault.
            return ToolResult(ok=False, error=str(exc))

        def save() -> tuple[int, datetime] | str:
            active = context.db.scalar(
                select(func.count())
                .select_from(ScheduledTask)
                .where(
                    ScheduledTask.user_id == context.user_id,
                    ScheduledTask.status == "active",
                )
            )
            if active >= settings.task_max_active:
                return (
                    f"There are already {active} active tasks, which is the "
                    "limit. Cancel one first."
                )

            next_run = schedules.compute_next_run(
                args.schedule_kind, args.interval_seconds, args.daily_at
            )
            task = ScheduledTask(
                user_id=context.user_id,
                name=args.name.strip(),
                prompt=args.prompt.strip(),
                schedule_kind=args.schedule_kind,
                interval_seconds=args.interval_seconds,
                daily_at=args.daily_at,
                next_run_at=next_run,
            )
            context.db.add(task)
            context.db.commit()
            return task.id, next_run

        outcome = await asyncio.to_thread(save)
        if isinstance(outcome, str):
            return ToolResult(ok=False, error=outcome)

        task_id, next_run = outcome
        when = schedules.describe(
            args.schedule_kind, args.interval_seconds, args.daily_at
        )
        logger.info("Created scheduled task %s (%s), first run %s", task_id, when, next_run)

        return ToolResult(
            output=(
                f"Task #{task_id} {args.name!r} scheduled {when}. "
                f"First run: {_format_local(next_run)}."
            )
        )


class NoArgs(BaseModel):
    pass


class ListScheduledTasks(Tool):
    name = "list_scheduled_tasks"
    description = (
        "Show the repeating tasks that are currently active, with when each "
        "next runs."
    )
    input_schema = NoArgs
    requires_confirmation = False

    def describe_action(self, args: NoArgs) -> str:
        return "List active scheduled tasks"

    async def run(self, args: NoArgs, context: ToolContext) -> ToolResult:
        if context.user_id is None:
            return ToolResult(ok=False, error="No user to list tasks for.")

        def load() -> list[tuple]:
            rows = context.db.scalars(
                select(ScheduledTask)
                .where(
                    ScheduledTask.user_id == context.user_id,
                    ScheduledTask.status == "active",
                )
                .order_by(ScheduledTask.next_run_at)
            ).all()
            return [
                (
                    r.id,
                    r.name,
                    schedules.describe(r.schedule_kind, r.interval_seconds, r.daily_at),
                    r.next_run_at,
                    r.prompt,
                )
                for r in rows
            ]

        tasks = await asyncio.to_thread(load)
        if not tasks:
            return ToolResult(output="No scheduled tasks are active.")

        lines = [
            f"#{tid}  {name} -- {when}, next {_format_local(nxt)}\n     {prompt}"
            for tid, name, when, nxt, prompt in tasks
        ]
        return ToolResult(output="\n".join(lines))


class CancelTaskInput(BaseModel):
    task_id: int = Field(description="The number shown by list_scheduled_tasks.")


class CancelScheduledTask(Tool):
    name = "cancel_scheduled_task"
    description = (
        "Stop a repeating task by its number. Call list_scheduled_tasks first "
        "if you do not know the number."
    )
    input_schema = CancelTaskInput

    # Stopping something is reversible by setting it up again, and nothing is
    # destroyed, so this does not ask. The audit log still records it.
    requires_confirmation = False

    def describe_action(self, args: CancelTaskInput) -> str:
        return f"Cancel scheduled task #{args.task_id}"

    async def run(self, args: CancelTaskInput, context: ToolContext) -> ToolResult:
        def cancel() -> str | None:
            task = context.db.get(ScheduledTask, args.task_id)
            if task is None or task.user_id != context.user_id:
                return None
            if task.status != "active":
                return f"already {task.status}"
            task.status = "cancelled"
            context.db.commit()
            return task.name

        outcome = await asyncio.to_thread(cancel)

        if outcome is None:
            return ToolResult(
                ok=False, error=f"There is no scheduled task #{args.task_id}."
            )
        if outcome.startswith("already "):
            return ToolResult(
                ok=False,
                error=f"Task #{args.task_id} was {outcome[8:]} already.",
            )
        return ToolResult(output=f"Cancelled scheduled task #{args.task_id}: {outcome}")


def build_scheduled_task_tools() -> list[Tool]:
    return [CreateScheduledTask(), ListScheduledTasks(), CancelScheduledTask()]
