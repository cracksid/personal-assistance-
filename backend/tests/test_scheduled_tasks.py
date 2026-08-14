"""
Tests for scheduled tasks -- the schedule arithmetic, the tools, and the
runner.

The interesting behaviour is about money and safety rather than storage:

  - a task must not run when nobody is there to read it (each run is a
    paid model call)
  - a broken task must not retry on every check
  - a destructive tool must be refused, not queued, when nobody can approve

Nothing sleeps. Due times are set in the past and run_due() is called
directly, which is exactly what the polling job does.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.automation import schedules
from app.automation.notifications import NotificationHub
from app.automation.task_runner import TaskRunner
from app.config import settings
from app.core.agent import AgentEvent
from app.db import crud
from app.db.models import Conversation, ScheduledTask
from app.tools.base import ToolContext
from app.tools.scheduled_tasks import (
    CancelScheduledTask,
    CancelTaskInput,
    CreateScheduledTask,
    CreateTaskInput,
    ListScheduledTasks,
    NoArgs,
)


class FakeClient:
    """Stands in for a WebSocket. Records what it was sent."""

    def __init__(self) -> None:
        self.received: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.received.append(data)


class FakeAgent:
    """
    An Agent-shaped object that yields a fixed reply.

    Records the unattended flag it was called with, which is the whole point
    of one of the tests below.
    """

    def __init__(self, reply: str = "Here is your briefing.", explode: bool = False):
        self.reply = reply
        self.explode = explode
        self.calls: list[tuple[str, bool]] = []

    async def respond(
        self, db, user_id, conversation_id, user_text, unattended=False
    ):
        self.calls.append((user_text, unattended))
        if self.explode:
            raise RuntimeError("the provider fell over")
        yield AgentEvent(type="text", text=self.reply)


@pytest.fixture
def owner(db_session):
    return crud.get_or_create_owner(db_session)


@pytest.fixture
def context(db_session, owner, memory_store) -> ToolContext:
    return ToolContext(db=db_session, user_id=owner.id, memory=memory_store)


def make_task(
    db,
    owner,
    name: str = "briefing",
    *,
    due_minutes_ago: int = 5,
    interval_seconds: int = 3600,
    status: str = "active",
) -> ScheduledTask:
    task = ScheduledTask(
        user_id=owner.id,
        name=name,
        prompt="Summarise my reminders.",
        schedule_kind="interval",
        interval_seconds=interval_seconds,
        next_run_at=schedules.utcnow_naive() - timedelta(minutes=due_minutes_ago),
        status=status,
    )
    db.add(task)
    db.commit()
    return task


def runner_for(db_engine, hub, agent) -> TaskRunner:
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    return TaskRunner(hub, factory, lambda: agent)


# --- schedule arithmetic ---------------------------------------------------


def test_a_daily_time_is_parsed():
    assert schedules.parse_daily_at("08:00") == (8, 0)
    assert schedules.parse_daily_at(" 23:59 ") == (23, 59)


@pytest.mark.parametrize("bad", ["8am", "08:00:00", "25:00", "08:61", "morning", "0800"])
def test_a_time_that_is_not_hh_mm_is_refused(bad):
    """
    Strict on purpose. Storing "8am" and discovering at 8am that it never
    fires is far worse than refusing it at the moment it is set.
    """
    with pytest.raises(schedules.ScheduleError):
        schedules.parse_daily_at(bad)


def test_a_daily_run_lands_on_that_local_time():
    when = schedules.next_daily_run("08:00")
    local = when.replace(tzinfo=timezone.utc).astimezone()

    assert (local.hour, local.minute) == (8, 0)
    assert when > schedules.utcnow_naive()


def test_a_daily_run_already_past_today_goes_to_tomorrow():
    """
    Otherwise a task that just ran at 08:00 would be due again immediately
    and loop all day.
    """
    just_after_8am_local = (
        datetime.now()
        .astimezone()
        .replace(hour=8, minute=0, second=30, microsecond=0)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )

    when = schedules.next_daily_run("08:00", after=just_after_8am_local)

    assert when - just_after_8am_local > timedelta(hours=23)


def test_an_interval_run_is_measured_from_the_end_of_the_last_one():
    now = schedules.utcnow_naive()
    assert schedules.next_interval_run(3600, after=now) == now + timedelta(hours=1)


def test_an_interval_below_the_minimum_is_refused():
    """
    This is a spending limit. Every run is a model call, so "every 5
    seconds" would burn money all night.
    """
    with pytest.raises(schedules.ScheduleError, match="shortest repeat"):
        schedules.validate("interval", settings.task_min_interval_seconds - 1, None)


def test_giving_both_schedule_forms_is_refused():
    with pytest.raises(schedules.ScheduleError, match="not both"):
        schedules.validate("interval", 3600, "08:00")


def test_giving_neither_schedule_form_is_refused():
    with pytest.raises(schedules.ScheduleError, match="needs"):
        schedules.validate("daily", None, None)


def test_an_unknown_schedule_kind_is_refused():
    with pytest.raises(schedules.ScheduleError, match="not a schedule kind"):
        schedules.validate("hourly-ish", 3600, None)


def test_a_schedule_is_described_in_words():
    assert schedules.describe("daily", None, "08:00") == "every day at 08:00"
    assert schedules.describe("interval", 3600, None) == "every 1 hour"
    assert schedules.describe("interval", 7200, None) == "every 2 hours"
    assert schedules.describe("interval", 900, None) == "every 15 minutes"


# --- creating --------------------------------------------------------------


@pytest.mark.anyio
async def test_a_task_is_stored_with_its_next_run(db_session, context):
    result = await CreateScheduledTask().run(
        CreateTaskInput(
            name="morning briefing",
            prompt="Summarise my pending reminders.",
            schedule_kind="daily",
            daily_at="08:00",
        ),
        context,
    )

    assert result.ok
    stored = db_session.scalars(select(ScheduledTask)).one()
    assert stored.name == "morning briefing"
    assert stored.status == "active"
    assert stored.next_run_at > schedules.utcnow_naive()


def test_creating_a_task_asks_for_confirmation():
    """
    A standing instruction that runs the agent loop on a timer, forever, is
    worth a human seeing once. Setup is the only moment one is guaranteed
    to be present.
    """
    assert CreateScheduledTask().requires_confirmation is True


def test_the_description_spells_out_the_schedule_and_the_prompt():
    description = CreateScheduledTask().describe_action(
        CreateTaskInput(
            name="hourly check",
            prompt="Check the news.",
            schedule_kind="interval",
            interval_seconds=3600,
        )
    )

    assert "every 1 hour" in description
    assert "Check the news." in description


@pytest.mark.anyio
async def test_a_too_frequent_task_is_refused(context):
    result = await CreateScheduledTask().run(
        CreateTaskInput(
            name="spam",
            prompt="hi",
            schedule_kind="interval",
            interval_seconds=5,
        ),
        context,
    )

    assert result.ok is False
    assert "shortest repeat" in result.error


@pytest.mark.anyio
async def test_the_number_of_active_tasks_is_capped(db_session, context, owner):
    for i in range(settings.task_max_active):
        make_task(db_session, owner, name=f"task {i}")

    result = await CreateScheduledTask().run(
        CreateTaskInput(
            name="one too many",
            prompt="hi",
            schedule_kind="daily",
            daily_at="08:00",
        ),
        context,
    )

    assert result.ok is False
    assert "limit" in result.error


# --- listing and cancelling ------------------------------------------------


@pytest.mark.anyio
async def test_listing_shows_only_active_tasks(db_session, context, owner):
    make_task(db_session, owner, "still running")
    make_task(db_session, owner, "stopped", status="cancelled")

    output = (await ListScheduledTasks().run(NoArgs(), context)).output

    assert "still running" in output
    assert "stopped" not in output


@pytest.mark.anyio
async def test_cancelling_stops_it_running(db_session, context, owner):
    task = make_task(db_session, owner, "cancel me")

    result = await CancelScheduledTask().run(CancelTaskInput(task_id=task.id), context)

    assert result.ok
    db_session.refresh(task)
    assert task.status == "cancelled"


@pytest.mark.anyio
async def test_cancelling_something_that_does_not_exist_says_so(context):
    result = await CancelScheduledTask().run(CancelTaskInput(task_id=9999), context)

    assert result.ok is False
    assert "no scheduled task" in result.error.lower()


# --- running ---------------------------------------------------------------


@pytest.mark.anyio
async def test_a_due_task_runs_and_pushes_its_answer(db_session, owner, db_engine):
    task = make_task(db_session, owner)
    hub = NotificationHub()
    client = FakeClient()
    hub.register(client)
    agent = FakeAgent("You have two reminders today.")

    assert await runner_for(db_engine, hub, agent).run_due() == 1

    assert client.received[0]["type"] == "task_result"
    assert client.received[0]["text"] == "You have two reminders today."
    assert client.received[0]["task_id"] == task.id


@pytest.mark.anyio
async def test_a_task_runs_unattended(db_session, owner, db_engine):
    """
    THE safety test. The flag is what makes core/gate.py refuse destructive
    tools instead of queueing an approval nobody is there to give.
    """
    make_task(db_session, owner)
    hub = NotificationHub()
    hub.register(FakeClient())
    agent = FakeAgent()

    await runner_for(db_engine, hub, agent).run_due()

    assert agent.calls == [("Summarise my reminders.", True)]


@pytest.mark.anyio
async def test_a_task_does_not_run_when_nobody_is_connected(
    db_session, owner, db_engine
):
    """
    THE money test, and the deliberate difference from a reminder.

    A reminder is already written, so it waits and is delivered late. A task
    has to be GENERATED, which costs a model call -- so it is not started at
    all until there is somebody to read the answer. Its due time is left
    untouched, so it fires on the next check after someone connects.
    """
    task = make_task(db_session, owner)
    due_before = task.next_run_at
    hub = NotificationHub()  # nobody listening
    agent = FakeAgent()

    assert await runner_for(db_engine, hub, agent).run_due() == 0
    assert agent.calls == []  # not a single model call

    db_session.refresh(task)
    assert task.next_run_at == due_before  # still due

    # Later, someone connects.
    hub.register(FakeClient())
    assert await runner_for(db_engine, hub, agent).run_due() == 1


@pytest.mark.anyio
async def test_the_schedule_moves_forward_after_a_run(db_session, owner, db_engine):
    task = make_task(db_session, owner, interval_seconds=3600)
    hub = NotificationHub()
    hub.register(FakeClient())

    await runner_for(db_engine, hub, FakeAgent()).run_due()

    db_session.refresh(task)
    assert task.last_run_at is not None
    assert task.next_run_at > schedules.utcnow_naive()


@pytest.mark.anyio
async def test_a_task_that_fails_still_moves_forward(db_session, owner, db_engine):
    """
    Otherwise a permanently broken task is due on every single check, and
    since a run is a model call, a broken task would spend money in a loop.
    """
    task = make_task(db_session, owner)
    hub = NotificationHub()
    hub.register(FakeClient())

    await runner_for(db_engine, hub, FakeAgent(explode=True)).run_due()

    db_session.refresh(task)
    assert task.next_run_at > schedules.utcnow_naive()


@pytest.mark.anyio
async def test_a_cancelled_task_does_not_run(db_session, owner, db_engine):
    make_task(db_session, owner, status="cancelled")
    hub = NotificationHub()
    hub.register(FakeClient())
    agent = FakeAgent()

    assert await runner_for(db_engine, hub, agent).run_due() == 0
    assert agent.calls == []


@pytest.mark.anyio
async def test_a_future_task_is_left_alone(db_session, owner, db_engine):
    make_task(db_session, owner, due_minutes_ago=-60)
    hub = NotificationHub()
    hub.register(FakeClient())

    assert await runner_for(db_engine, hub, FakeAgent()).run_due() == 0


@pytest.mark.anyio
async def test_a_task_gets_its_own_conversation(db_session, owner, db_engine):
    """
    So a daily briefing builds up its own history without filling the
    user's real chat with turns they never sent.
    """
    task = make_task(db_session, owner, "briefing")
    hub = NotificationHub()
    hub.register(FakeClient())

    await runner_for(db_engine, hub, FakeAgent()).run_due()

    db_session.refresh(task)
    assert task.conversation_id is not None

    conversation = db_session.get(Conversation, task.conversation_id)
    assert conversation.title == "Scheduled: briefing"


@pytest.mark.anyio
async def test_a_task_conversation_is_never_resumed_as_the_users_chat(
    db_session, owner, db_engine
):
    """
    Regression test for a live bug, and the reason Conversation.kind exists.

    Giving a task its own conversation kept its turns out of the user's
    chat -- but only in one direction. Resuming "the newest conversation"
    picked up the task's thread, so the user's next message was appended to
    it and the model answered out of a history the user had never seen.

    Observed for real: a chat resumed "Scheduled: pulse" and started
    replying from the task's context.
    """
    make_task(db_session, owner, "pulse")
    hub = NotificationHub()
    hub.register(FakeClient())

    await runner_for(db_engine, hub, FakeAgent()).run_due()

    # The task's conversation is now the newest one that exists.
    newest = db_session.scalars(
        select(Conversation).order_by(Conversation.id.desc())
    ).first()
    assert newest.kind == "task"

    # ...and the user's chat must still not land in it.
    resumed = crud.get_or_create_active_conversation(db_session, owner)
    assert resumed.id != newest.id
    assert resumed.kind == "chat"


@pytest.mark.anyio
async def test_the_same_conversation_is_reused_on_the_next_run(
    db_session, owner, db_engine
):
    task = make_task(db_session, owner, interval_seconds=300)
    hub = NotificationHub()
    hub.register(FakeClient())
    runner = runner_for(db_engine, hub, FakeAgent())

    await runner.run_due()
    db_session.refresh(task)
    first = task.conversation_id

    # Make it due again rather than waiting five minutes.
    task.next_run_at = schedules.utcnow_naive() - timedelta(seconds=1)
    db_session.commit()
    await runner.run_due()

    db_session.refresh(task)
    assert task.conversation_id == first
    assert len(db_session.scalars(select(Conversation)).all()) == 1


@pytest.mark.anyio
async def test_an_empty_reply_is_not_broadcast(db_session, owner, db_engine):
    """A blank push would look like a bug to the user. Say nothing instead."""
    make_task(db_session, owner)
    hub = NotificationHub()
    client = FakeClient()
    hub.register(client)

    await runner_for(db_engine, hub, FakeAgent(reply="   ")).run_due()

    assert client.received == []
