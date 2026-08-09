"""
Reminder tools.

WHY THESE DO NOT PARSE ENGLISH.

The obvious design is a tool that accepts "next Friday evening" and parses
it. That means a date-parsing dependency guessing at whose week starts on
Sunday and what "evening" means -- ambiguity pushed into a library that
cannot ask.

Instead the caller says when in one of two unambiguous ways:

    due_in_seconds  -- "in 10 minutes" is 600
    due_at          -- an ISO 8601 instant with an offset

Both were needed, and the second one alone was not enough. The first design
accepted only due_at, reasoning that a model with get_current_time can work
out any instant. That held for absolute times and broke immediately on a
relative one: asked to remind "in 40 seconds", Claude sent
due_at="40 seconds". Perfectly sensible English, not a timestamp -- and
demanding date arithmetic for "in ten minutes" was simply the wrong ask.

The residual cost is honest: a model that miscalculates an absolute date
schedules the wrong moment. list_reminders exists partly so that is visible
rather than silent, and describe_action spells the resulting time back out
in full so a wrong date can be caught by reading it.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import settings
from app.db.models import Reminder
from app.tools.base import Tool, ToolContext, ToolError, ToolResult

logger = logging.getLogger(__name__)


def _to_utc_naive(raw: str) -> datetime:
    """
    Parse an ISO timestamp into naive UTC, the form the database uses.

    Accepts both "2026-08-09T21:00:00" (assumed local) and
    "2026-08-09T21:00:00+05:30" (explicit offset). An explicit offset is
    preferable and what the model is asked for, since "21:00" alone is
    ambiguous the moment anything crosses a timezone.
    """
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ToolError(
            f"{raw!r} is not a valid ISO timestamp. Use e.g. 2026-08-09T21:00:00+05:30."
        ) from exc

    if parsed.tzinfo is None:
        # No offset given: interpret as the machine's local time, which is
        # what a user means by "9pm". astimezone() attaches the local zone.
        parsed = parsed.astimezone()

    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _format_local(when_utc: datetime) -> str:
    """Render a stored naive-UTC instant back in the user's local time."""
    aware = when_utc.replace(tzinfo=timezone.utc)
    return aware.astimezone().strftime("%A, %d %B %Y at %H:%M")


class CreateReminderInput(BaseModel):
    """
    Two ways to say when, because models reach for both.

    The original design accepted only an ISO timestamp, on the theory that
    converting "tonight" into an instant is the model's job. That holds for
    absolute times. It failed live on a relative one: asked for a reminder
    "in 40 seconds", Claude sent due_at="40 seconds" -- which is a perfectly
    reasonable thing to say and not a timestamp at all.

    Forcing date arithmetic for "in ten minutes" was the wrong ask. Now
    either form works, and the tool does the conversion it is better suited
    to than the model.
    """

    message: str = Field(
        description="What to remind the user about, phrased as you would say it."
    )
    due_in_seconds: int | None = Field(
        default=None,
        description=(
            "For a relative time -- 'in 10 minutes' is 600. Prefer this "
            "whenever the user says 'in X'; it needs no date arithmetic."
        ),
    )
    due_at: str | None = Field(
        default=None,
        description=(
            "For an absolute time, as an ISO 8601 timestamp WITH a timezone "
            "offset, e.g. '2026-08-09T21:00:00+05:30'. Call get_current_time "
            "first so you know today's date. Use this OR due_in_seconds, "
            "not both."
        ),
    )

    def resolve(self) -> datetime:
        """Work out the absolute instant, in naive UTC, from whichever was given."""
        if self.due_in_seconds is not None and self.due_at is not None:
            raise ToolError("Give either due_in_seconds or due_at, not both.")
        if self.due_in_seconds is not None:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            return now + timedelta(seconds=self.due_in_seconds)
        if self.due_at is not None:
            return _to_utc_naive(self.due_at)
        raise ToolError("Say when: give due_in_seconds or due_at.")


class CreateReminder(Tool):
    name = "create_reminder"
    description = (
        "Schedule a reminder. Use this whenever the user asks to be reminded "
        "of something later. For 'in 10 minutes' use due_in_seconds=600. For "
        "'at 9pm tonight' call get_current_time first, then pass due_at as a "
        "full ISO timestamp with offset."
    )
    input_schema = CreateReminderInput

    # Creating a reminder is additive and easily cancelled, so no
    # confirmation. It is still audited like everything else.
    requires_confirmation = False

    def describe_action(self, args: CreateReminderInput) -> str:
        try:
            when = _format_local(args.resolve())
        except ToolError:
            when = args.due_at or f"in {args.due_in_seconds}s"
        return f"Remind you on {when}: {args.message}"

    async def run(self, args: CreateReminderInput, context: ToolContext) -> ToolResult:
        if context.user_id is None:
            return ToolResult(ok=False, error="No user to set a reminder for.")

        due_at = args.resolve()
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        if due_at <= now:
            # Almost always a model miscalculating the date. Say so plainly
            # rather than storing something that fires immediately.
            return ToolResult(
                ok=False,
                error=(
                    f"{_format_local(due_at)} is in the past. Check the current "
                    "time and try again."
                ),
            )

        limit = now + timedelta(days=settings.reminder_max_days_ahead)
        if due_at > limit:
            return ToolResult(
                ok=False,
                error=(
                    f"{_format_local(due_at)} is more than "
                    f"{settings.reminder_max_days_ahead} days away, which is "
                    "probably a mistake in the date."
                ),
            )

        def save() -> int:
            reminder = Reminder(
                user_id=context.user_id, message=args.message.strip(), due_at=due_at
            )
            context.db.add(reminder)
            context.db.commit()
            return reminder.id

        reminder_id = await asyncio.to_thread(save)
        logger.info("Created reminder %s for %s", reminder_id, due_at)

        return ToolResult(
            output=f"Reminder #{reminder_id} set for {_format_local(due_at)}: {args.message}"
        )


class NoArgs(BaseModel):
    pass


class ListReminders(Tool):
    name = "list_reminders"
    description = (
        "Show the reminders that are still waiting to fire. Use this when "
        "the user asks what reminders they have."
    )
    input_schema = NoArgs
    requires_confirmation = False

    def describe_action(self, args: NoArgs) -> str:
        return "List pending reminders"

    async def run(self, args: NoArgs, context: ToolContext) -> ToolResult:
        if context.user_id is None:
            return ToolResult(ok=False, error="No user to list reminders for.")

        def load() -> list[tuple[int, str, datetime]]:
            rows = context.db.scalars(
                select(Reminder)
                .where(
                    Reminder.user_id == context.user_id,
                    Reminder.status == "pending",
                )
                .order_by(Reminder.due_at)
            ).all()
            return [(r.id, r.message, r.due_at) for r in rows]

        pending = await asyncio.to_thread(load)
        if not pending:
            return ToolResult(output="No reminders are pending.")

        lines = [
            f"#{rid}  {_format_local(due)}  -  {message}"
            for rid, message, due in pending
        ]
        return ToolResult(output="\n".join(lines))


class CancelReminderInput(BaseModel):
    reminder_id: int = Field(description="The number shown by list_reminders.")


class CancelReminder(Tool):
    name = "cancel_reminder"
    description = (
        "Cancel a pending reminder by its number. Call list_reminders first "
        "if you do not know the number."
    )
    input_schema = CancelReminderInput
    requires_confirmation = False

    def describe_action(self, args: CancelReminderInput) -> str:
        return f"Cancel reminder #{args.reminder_id}"

    async def run(self, args: CancelReminderInput, context: ToolContext) -> ToolResult:
        def cancel() -> str | None:
            reminder = context.db.get(Reminder, args.reminder_id)
            if reminder is None or reminder.user_id != context.user_id:
                return None
            if reminder.status != "pending":
                return f"already {reminder.status}"
            reminder.status = "cancelled"
            context.db.commit()
            return reminder.message

        outcome = await asyncio.to_thread(cancel)

        if outcome is None:
            return ToolResult(
                ok=False, error=f"There is no reminder #{args.reminder_id}."
            )
        if outcome.startswith("already "):
            return ToolResult(
                ok=False,
                error=f"Reminder #{args.reminder_id} was {outcome[8:]}, so there is nothing to cancel.",
            )
        return ToolResult(output=f"Cancelled reminder #{args.reminder_id}: {outcome}")


def build_reminder_tools() -> list[Tool]:
    return [CreateReminder(), ListReminders(), CancelReminder()]
