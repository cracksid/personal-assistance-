"""
Working out when a repeating task should next run.

Kept separate from both the tool that creates tasks and the runner that
executes them, because both need the same arithmetic and neither should own
it. Pure functions, no database, no imports from tools/ or core/ -- which
makes this the easiest file in the phase to test.

TWO KINDS OF SCHEDULE, AND WHY BOTH.

    interval    every N seconds. Simple, exact, and drifts nowhere.
    daily       at a wall-clock time, e.g. "08:00".

"Every 6 hours" and "every morning at 8" sound similar and are not the same
thing. The first is a duration; the second is a position in the day. Storing
"every morning" as 86400 seconds from the first run works right up until the
clocks change or a restart shifts the anchor, and then breakfast reminders
arrive at 7am forever.
"""

from datetime import datetime, timedelta, timezone

from app.config import settings

# The two allowed values of ScheduledTask.schedule_kind.
INTERVAL = "interval"
DAILY = "daily"


class ScheduleError(ValueError):
    """A schedule that cannot be honoured. The message is shown to the user."""


def utcnow_naive() -> datetime:
    """Current UTC with the tzinfo stripped, matching what SQLite returns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_daily_at(raw: str) -> tuple[int, int]:
    """
    Validate an "HH:MM" wall-clock time and return it as (hour, minute).

    Deliberately strict. A model that sends "8am" or "08:00:00" is told so
    immediately, rather than having it stored and silently never firing.
    """
    text = raw.strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ScheduleError(f"{raw!r} is not a time. Use 24-hour HH:MM, e.g. '08:00'.")

    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        raise ScheduleError(
            f"{raw!r} is not a time. Use 24-hour HH:MM, e.g. '08:00'."
        ) from None

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(f"{raw!r} is not a real time of day.")
    return hour, minute


def next_daily_run(daily_at: str, after: datetime | None = None) -> datetime:
    """
    The next time "HH:MM" comes round in the user's local timezone, as naive UTC.

    `after` is a naive-UTC instant; the result is always strictly later than
    it, so a task that just ran at 08:00 is scheduled for 08:00 tomorrow
    rather than immediately again.

    HONEST LIMITATION: astimezone() with no argument gives a *fixed* offset
    taken from the current moment, so on the one day a year the clocks
    change, the next run can land an hour out. It corrects itself on the
    following run, because each calculation starts from a fresh offset.
    Fixing it properly means asking the user for an IANA zone name and
    carrying zoneinfo, which is a real cost for a once-a-year hour.
    """
    hour, minute = parse_daily_at(daily_at)

    reference = utcnow_naive() if after is None else after
    local = reference.replace(tzinfo=timezone.utc).astimezone()

    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)

    return candidate.astimezone(timezone.utc).replace(tzinfo=None)


def next_interval_run(interval_seconds: int, after: datetime | None = None) -> datetime:
    """
    `interval_seconds` from `after`.

    Measured from when the run finished rather than from when it was due.
    A task that took two minutes therefore waits its full interval before
    going again, instead of a slow task effectively running back to back.
    """
    reference = utcnow_naive() if after is None else after
    return reference + timedelta(seconds=interval_seconds)


def validate(
    schedule_kind: str, interval_seconds: int | None, daily_at: str | None
) -> None:
    """
    Check a schedule before it reaches the database.

    Raises ScheduleError with a sentence written for a human -- the model
    reads it too, and a clear message is what lets it correct itself on the
    next attempt instead of retrying the same broken call.
    """
    if schedule_kind == INTERVAL:
        if interval_seconds is None:
            raise ScheduleError("An interval schedule needs interval_seconds.")
        if daily_at is not None:
            raise ScheduleError("Give interval_seconds or daily_at, not both.")
        if interval_seconds < settings.task_min_interval_seconds:
            # Not arbitrary: every run is a paid model call.
            raise ScheduleError(
                f"The shortest repeat allowed is "
                f"{settings.task_min_interval_seconds} seconds, because every "
                "run costs a model call. Use a reminder for anything sooner."
            )

    elif schedule_kind == DAILY:
        if daily_at is None:
            raise ScheduleError("A daily schedule needs daily_at, e.g. '08:00'.")
        if interval_seconds is not None:
            raise ScheduleError("Give interval_seconds or daily_at, not both.")
        parse_daily_at(daily_at)

    else:
        raise ScheduleError(
            f"{schedule_kind!r} is not a schedule kind. Use 'interval' or 'daily'."
        )


def compute_next_run(
    schedule_kind: str,
    interval_seconds: int | None,
    daily_at: str | None,
    after: datetime | None = None,
) -> datetime:
    """Work out the next run for an already-validated schedule."""
    if schedule_kind == DAILY:
        return next_daily_run(daily_at or "", after)
    return next_interval_run(interval_seconds or 0, after)


def describe(schedule_kind: str, interval_seconds: int | None, daily_at: str | None) -> str:
    """A human phrase for a schedule, for descriptions and listings."""
    if schedule_kind == DAILY:
        return f"every day at {daily_at}"

    seconds = interval_seconds or 0
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"every {hours} hour{'s' if hours != 1 else ''}"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"every {minutes} minute{'s' if minutes != 1 else ''}"
    return f"every {seconds} seconds"
