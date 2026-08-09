"""
The background scheduler.

CLAUDE.md locks APScheduler rather than Celery: a single-user desktop app
should not need a message broker running alongside it.

ONE POLLING JOB, NOT ONE JOB PER REMINDER.

The obvious design is to hand APScheduler a job for each reminder, firing at
its exact due time. This does the opposite: a single job runs every few
seconds and asks the database "is anything due?".

Polling is less precise -- a reminder can be a few seconds late. It buys
something worth far more on a desktop machine: the scheduler holds no state
at all. Reminders live in SQLite, so closing the laptop, restarting the
server, or crashing loses nothing, and no code has to re-register jobs on
startup. With per-reminder jobs, every one of those events would need
careful recovery logic.

THE TIMEZONE TRAP.

SQLite has no real datetime type and hands values back timezone-NAIVE, as
db/models.py warns. Comparing a naive value from the database against an
aware datetime.now(timezone.utc) raises TypeError. Everything here is
therefore naive UTC, converted at the edges -- never local time, which is
ambiguous twice a year.
"""

import asyncio
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automation.notifications import NotificationHub
from app.config import settings
from app.db.models import Reminder

logger = logging.getLogger(__name__)


def utcnow_naive() -> datetime:
    """
    The current UTC time, without a timezone tag.

    Matches what SQLite gives back, so comparisons work. Using
    datetime.now() instead would silently compare local time against stored
    UTC and fire reminders hours early or late.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ReminderScheduler:
    """Checks for due reminders and pushes them to whoever is listening."""

    def __init__(self, hub: NotificationHub, session_factory) -> None:
        """
        Args:
            hub: where to send reminders that come due.
            session_factory: called to get a database session. The scheduler
                makes its own -- it runs on a timer, not inside a request,
                so there is no request-scoped session to borrow.
        """
        self._hub = hub
        self._session_factory = session_factory
        self._scheduler = AsyncIOScheduler()

    def start(self) -> None:
        self._scheduler.add_job(
            self.deliver_due,
            "interval",
            seconds=settings.reminder_check_seconds,
            id="deliver_due_reminders",
            # If a check overruns or the machine was asleep, run once on
            # waking rather than firing every missed interval at once.
            coalesce=True,
            max_instances=1,
        )
        self._scheduler.start()
        logger.info(
            "Scheduler started (checking reminders every %ss)",
            settings.reminder_check_seconds,
        )

    def shutdown(self) -> None:
        if self._scheduler.running:
            # wait=False: do not block server shutdown on an in-flight check.
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    async def deliver_due(self) -> int:
        """
        Deliver every reminder that has come due. Returns how many were sent.

        Safe to call directly, which is what the tests do and what a
        WebSocket does on connect to pick up anything missed while the app
        was closed.
        """
        try:
            due = await asyncio.to_thread(self._find_due)
            if not due:
                return 0

            delivered = 0
            for reminder_id, message, due_at in due:
                receivers = await self._hub.broadcast(
                    {
                        "type": "reminder",
                        "id": reminder_id,
                        "message": message,
                        "due_at": due_at.isoformat(),
                    }
                )
                if receivers:
                    await asyncio.to_thread(self._mark_delivered, reminder_id)
                    delivered += 1
                    logger.info("Delivered reminder %s to %s client(s)", reminder_id, receivers)
                else:
                    # Nobody was listening. Leave it pending so it arrives
                    # when someone connects, rather than vanishing.
                    logger.info("Reminder %s is due but nobody is connected", reminder_id)

            return delivered

        except Exception:
            # A scheduled job that raises would otherwise die silently, and
            # APScheduler would keep calling it forever with no visible
            # cause. Log the traceback and carry on.
            logger.error("Reminder check failed", exc_info=True)
            return 0

    def _find_due(self) -> list[tuple[int, str, datetime]]:
        """
        Read due reminders out into plain tuples.

        Deliberately not returning ORM objects: they belong to a session
        that closes when this function returns, and touching a detached
        instance afterwards raises. Copying the three fields we need avoids
        the problem entirely.
        """
        db: Session = self._session_factory()
        try:
            rows = db.scalars(
                select(Reminder)
                .where(Reminder.status == "pending", Reminder.due_at <= utcnow_naive())
                .order_by(Reminder.due_at)
            ).all()
            return [(r.id, r.message, r.due_at) for r in rows]
        finally:
            db.close()

    def _mark_delivered(self, reminder_id: int) -> None:
        db: Session = self._session_factory()
        try:
            reminder = db.get(Reminder, reminder_id)
            if reminder is not None and reminder.status == "pending":
                reminder.status = "delivered"
                reminder.delivered_at = utcnow_naive()
                db.commit()
        finally:
            db.close()
