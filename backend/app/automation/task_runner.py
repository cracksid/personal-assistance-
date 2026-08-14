"""
Running scheduled tasks.

Same polling design as ReminderScheduler, and for the same reason: no state
outside the database, so sleeping the laptop costs nothing and no recovery
code is needed on startup.

WHAT MAKES THIS DIFFERENT FROM A REMINDER.

A reminder is a string. Delivering it is a broadcast. A task is a PROMPT: it
goes through the full agent loop -- memory, tools, the lot -- and the reply
is what gets delivered. Two consequences fall out of that, and both shape
this file:

1. It costs money and time. So a task is not run when nobody is connected
   to receive the answer. It waits, and fires on the next check after
   someone connects. Generating a morning briefing into an empty room and
   billing for it would be worse than being a few minutes late.

   Note the difference from a reminder, which is *delivered* late but was
   already written. A task is not even *started* until there is an audience.

2. Nobody is there to approve anything. The turn is run with
   unattended=True, and core/gate.py refuses destructive tools outright.
   That rule lives in the gate, not here -- this file only sets the flag.

WHY A FAILED TASK STILL MOVES FORWARD.

If a run raises, next_run_at is advanced anyway. Leaving it in the past
would make a permanently broken task retry on every single check, which for
something that calls a model means a broken task quietly spending money in
a loop.
"""

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automation import schedules
from app.automation.notifications import NotificationHub
from app.db.models import Conversation, ScheduledTask

logger = logging.getLogger(__name__)


@dataclass
class _DueTask:
    """
    A due task copied out of the session that read it.

    Same reasoning as ReminderScheduler._find_due: the ORM object belongs to
    a session that closes when the read returns, and touching a detached
    instance later raises. Copying the fields needed avoids it entirely.
    """

    id: int
    user_id: int
    name: str
    prompt: str
    conversation_id: int | None


class TaskRunner:
    """Runs scheduled tasks that have come due."""

    def __init__(
        self,
        hub: NotificationHub,
        session_factory,
        agent_factory,
    ) -> None:
        """
        Args:
            hub: where finished results are pushed.
            session_factory: called for a database session. The runner makes
                its own -- it runs on a timer, not inside a request.
            agent_factory: called for an Agent. A callable rather than an
                Agent so this module never learns which provider is
                configured, and so tests can pass a fake in one line.
        """
        self._hub = hub
        self._session_factory = session_factory
        self._agent_factory = agent_factory

    async def run_due(self) -> int:
        """
        Run every task that has come due. Returns how many ran.

        Safe to call directly, which is what the tests do.
        """
        try:
            due = await asyncio.to_thread(self._find_due)
            if not due:
                return 0

            if self._hub.client_count == 0:
                # Nobody to read the answer, so do not generate one. Leave
                # every due time exactly as it is; these run on the check
                # after someone connects.
                #
                # The database is queried BEFORE this check, even though
                # checking clients first would be cheaper, so the log can say
                # which tasks are waiting and why. A silent return every 30
                # seconds would make "waiting for you" indistinguishable from
                # "the runner is broken" -- a distinction that cost real time
                # to debug when fact extraction logged nothing on zero facts.
                logger.info(
                    "%s task(s) due but nobody is connected: %s",
                    len(due),
                    ", ".join(t.name for t in due),
                )
                return 0

            ran = 0
            for task in due:
                await self._run_one(task)
                ran += 1
            return ran

        except Exception:
            # A scheduled job that raises would otherwise die silently while
            # APScheduler kept calling it forever with no visible cause.
            logger.error("Scheduled task check failed", exc_info=True)
            return 0

    async def _run_one(self, task: _DueTask) -> None:
        """Run one task's prompt through the agent and push the answer."""
        logger.info("Running scheduled task %s (%s)", task.id, task.name)

        db: Session = self._session_factory()
        try:
            conversation_id = await asyncio.to_thread(
                self._conversation_for, db, task
            )

            reply_parts: list[str] = []
            tools_used: list[str] = []

            try:
                agent = self._agent_factory()
                async for event in agent.respond(
                    db,
                    task.user_id,
                    conversation_id,
                    task.prompt,
                    unattended=True,
                ):
                    if event.type == "text":
                        reply_parts.append(event.text)
                    elif event.type == "tool":
                        tools_used.append(event.tool_name or "?")
                    # type="confirmation" cannot occur: the gate refuses
                    # rather than asks when unattended.

            except Exception:
                logger.error("Scheduled task %s failed", task.id, exc_info=True)
                reply_parts = []

            reply = "".join(reply_parts).strip()

            if reply:
                await self._hub.broadcast(
                    {
                        "type": "task_result",
                        "task_id": task.id,
                        "name": task.name,
                        "text": reply,
                        "tools_used": tools_used,
                    }
                )
                logger.info(
                    "Task %s produced %s characters using %s tool(s)",
                    task.id,
                    len(reply),
                    len(tools_used),
                )
            else:
                logger.warning("Task %s produced nothing", task.id)

            # Advance the schedule whatever happened -- see the module
            # docstring on why a failure must not retry immediately.
            await asyncio.to_thread(self._advance, db, task.id)

        finally:
            db.close()

    # --- database ----------------------------------------------------------

    def _find_due(self) -> list[_DueTask]:
        db: Session = self._session_factory()
        try:
            rows = db.scalars(
                select(ScheduledTask)
                .where(
                    ScheduledTask.status == "active",
                    ScheduledTask.next_run_at <= schedules.utcnow_naive(),
                )
                .order_by(ScheduledTask.next_run_at)
            ).all()
            return [
                _DueTask(
                    id=r.id,
                    user_id=r.user_id,
                    name=r.name,
                    prompt=r.prompt,
                    conversation_id=r.conversation_id,
                )
                for r in rows
            ]
        finally:
            db.close()

    def _conversation_for(self, db: Session, task: _DueTask) -> int:
        """
        The task's own conversation, created on first run.

        Each task gets its own rather than writing into the user's live chat.
        A morning briefing then builds up its own short history -- so it can
        say "same as yesterday" -- without the user's actual conversation
        filling with machine-generated turns they never sent.
        """
        if task.conversation_id is not None:
            return task.conversation_id

        conversation = Conversation(
            user_id=task.user_id,
            title=f"Scheduled: {task.name}",
            # kind="task" keeps this thread out of the user's chat. Without
            # it, resuming "the newest conversation" picked up this one and
            # the user found themselves talking inside a task's history.
            kind="task",
        )
        db.add(conversation)
        db.commit()

        row = db.get(ScheduledTask, task.id)
        if row is not None:
            row.conversation_id = conversation.id
            db.commit()

        return conversation.id

    def _advance(self, db: Session, task_id: int) -> None:
        """Record the run and work out when to go again."""
        row = db.get(ScheduledTask, task_id)
        if row is None or row.status != "active":
            return

        now = schedules.utcnow_naive()
        row.last_run_at = now
        row.next_run_at = schedules.compute_next_run(
            row.schedule_kind, row.interval_seconds, row.daily_at, after=now
        )
        db.commit()
        logger.info("Task %s next runs at %s", task_id, row.next_run_at)
