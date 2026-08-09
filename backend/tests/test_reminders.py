"""
Tests for reminders and the scheduler.

The interesting behaviour is not "does a row get written" but the timing
rules around it: a reminder that comes due while nobody is connected must
not be lost, one that is delivered must not be delivered twice, and a model
that miscalculates a date must be told rather than obeyed.

Nothing here sleeps. Waiting for a real 20-second scheduler tick would make
the suite unbearable, so due times are set in the past and deliver_due() is
called directly -- which is also exactly what a WebSocket does on connect.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.automation.notifications import NotificationHub
from app.automation.scheduler import ReminderScheduler, utcnow_naive
from app.db import crud
from app.db.models import Reminder
from app.tools.base import ToolContext, ToolError
from app.tools.reminders import (
    CancelReminder,
    CancelReminderInput,
    CreateReminder,
    CreateReminderInput,
    ListReminders,
    NoArgs,
    _to_utc_naive,
)


class FakeClient:
    """Stands in for a WebSocket. Records what it was sent."""

    def __init__(self, broken: bool = False) -> None:
        self.received: list[dict] = []
        self.broken = broken

    async def send_json(self, data: dict) -> None:
        if self.broken:
            raise ConnectionError("socket closed")
        self.received.append(data)


@pytest.fixture
def owner(db_session):
    return crud.get_or_create_owner(db_session)


@pytest.fixture
def context(db_session, owner, memory_store) -> ToolContext:
    return ToolContext(db=db_session, user_id=owner.id, memory=memory_store)


def iso_in(**delta) -> str:
    """An ISO timestamp with an explicit offset, relative to now."""
    return (datetime.now().astimezone() + timedelta(**delta)).isoformat()


def make_reminder(db, owner, message: str, *, minutes_ago: int = 0) -> Reminder:
    reminder = Reminder(
        user_id=owner.id,
        message=message,
        due_at=utcnow_naive() - timedelta(minutes=minutes_ago),
    )
    db.add(reminder)
    db.commit()
    return reminder


# --- parsing time ----------------------------------------------------------


def test_an_offset_aware_timestamp_is_converted_to_utc():
    """
    The model is asked for an explicit offset. 21:00 in +05:30 is 15:30 UTC,
    and that is what must be stored -- otherwise a reminder fires hours off.
    """
    assert _to_utc_naive("2026-08-09T21:00:00+05:30") == datetime(2026, 8, 9, 15, 30)


def test_a_z_suffix_is_understood():
    assert _to_utc_naive("2026-08-09T15:30:00Z") == datetime(2026, 8, 9, 15, 30)


def test_a_naive_timestamp_is_read_as_local_time():
    """
    "9pm" with no offset means 9pm where the user is. Treating it as UTC
    instead would fire at the wrong hour for anyone outside London.
    """
    local_9pm = datetime(2026, 8, 9, 21, 0).astimezone()
    expected = local_9pm.astimezone(timezone.utc).replace(tzinfo=None)

    assert _to_utc_naive("2026-08-09T21:00:00") == expected


def test_nonsense_is_rejected_with_an_example():
    with pytest.raises(ToolError, match="not a valid ISO timestamp"):
        _to_utc_naive("tomorrow evening")


# --- creating --------------------------------------------------------------


@pytest.mark.anyio
async def test_a_reminder_is_stored_and_pending(db_session, context, owner):
    result = await CreateReminder().run(
        CreateReminderInput(message="practise guitar", due_at=iso_in(hours=2)), context
    )

    assert result.ok
    stored = db_session.scalars(select(Reminder)).all()
    assert [(r.message, r.status) for r in stored] == [("practise guitar", "pending")]


@pytest.mark.anyio
async def test_a_time_in_the_past_is_refused(context):
    """
    Almost always a model getting today's date wrong. Storing it would fire
    instantly, which looks like a bug to the user; saying so is honest.
    """
    result = await CreateReminder().run(
        CreateReminderInput(message="too late", due_at=iso_in(hours=-1)), context
    )

    assert result.ok is False
    assert "in the past" in result.error


@pytest.mark.anyio
async def test_an_absurdly_distant_time_is_refused(context):
    result = await CreateReminder().run(
        CreateReminderInput(message="the year 3000", due_at="3000-01-01T09:00:00+00:00"),
        context,
    )

    assert result.ok is False
    assert "days away" in result.error


def test_the_description_spells_the_time_back_out():
    """
    The user should be able to catch a wrong date by reading it, so the
    description renders the instant in local time rather than echoing the
    ISO string back.
    """
    description = CreateReminder().describe_action(
        CreateReminderInput(message="call mum", due_at="2026-08-09T21:00:00+05:30")
    )

    assert "call mum" in description
    assert "2026" in description
    assert "T21:00" not in description  # not the raw ISO input


# --- listing and cancelling ------------------------------------------------


@pytest.mark.anyio
async def test_listing_shows_only_pending_reminders(db_session, context, owner):
    make_reminder(db_session, owner, "still waiting", minutes_ago=-60)
    done = make_reminder(db_session, owner, "already done", minutes_ago=-60)
    done.status = "delivered"
    db_session.commit()

    output = (await ListReminders().run(NoArgs(), context)).output

    assert "still waiting" in output
    assert "already done" not in output


@pytest.mark.anyio
async def test_cancelling_stops_it_firing(db_session, context, owner):
    reminder = make_reminder(db_session, owner, "cancel me", minutes_ago=-60)

    result = await CancelReminder().run(
        CancelReminderInput(reminder_id=reminder.id), context
    )

    assert result.ok
    db_session.refresh(reminder)
    assert reminder.status == "cancelled"


@pytest.mark.anyio
async def test_cancelling_something_that_does_not_exist_says_so(context):
    result = await CancelReminder().run(CancelReminderInput(reminder_id=9999), context)

    assert result.ok is False
    assert "no reminder" in result.error.lower()


# --- delivery --------------------------------------------------------------


@pytest.mark.anyio
async def test_a_due_reminder_is_pushed_and_marked_delivered(
    db_session, owner, db_engine
):
    from sqlalchemy.orm import sessionmaker

    reminder = make_reminder(db_session, owner, "guitar time", minutes_ago=5)
    hub = NotificationHub()
    client = FakeClient()
    hub.register(client)

    scheduler = ReminderScheduler(
        hub, sessionmaker(bind=db_engine, expire_on_commit=False)
    )
    sent = await scheduler.deliver_due()

    assert sent == 1
    assert client.received[0]["type"] == "reminder"
    assert client.received[0]["message"] == "guitar time"

    db_session.refresh(reminder)
    assert reminder.status == "delivered"


@pytest.mark.anyio
async def test_a_reminder_is_not_lost_when_nobody_is_connected(
    db_session, owner, db_engine
):
    """
    THE test for this design. A laptop shut at 8pm must not swallow a 9pm
    reminder -- it stays pending and arrives when you next connect.
    """
    from sqlalchemy.orm import sessionmaker

    reminder = make_reminder(db_session, owner, "important", minutes_ago=5)
    hub = NotificationHub()  # nobody listening
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    scheduler = ReminderScheduler(hub, factory)

    assert await scheduler.deliver_due() == 0
    db_session.refresh(reminder)
    assert reminder.status == "pending"  # NOT marked delivered

    # Later, someone connects.
    client = FakeClient()
    hub.register(client)

    assert await scheduler.deliver_due() == 1
    assert client.received[0]["message"] == "important"


@pytest.mark.anyio
async def test_a_reminder_is_only_delivered_once(db_session, owner, db_engine):
    from sqlalchemy.orm import sessionmaker

    make_reminder(db_session, owner, "once only", minutes_ago=5)
    hub = NotificationHub()
    client = FakeClient()
    hub.register(client)
    scheduler = ReminderScheduler(
        hub, sessionmaker(bind=db_engine, expire_on_commit=False)
    )

    await scheduler.deliver_due()
    await scheduler.deliver_due()  # the very next tick

    assert len(client.received) == 1


@pytest.mark.anyio
async def test_a_future_reminder_is_left_alone(db_session, owner, db_engine):
    from sqlalchemy.orm import sessionmaker

    make_reminder(db_session, owner, "not yet", minutes_ago=-60)
    hub = NotificationHub()
    hub.register(FakeClient())
    scheduler = ReminderScheduler(
        hub, sessionmaker(bind=db_engine, expire_on_commit=False)
    )

    assert await scheduler.deliver_due() == 0


# --- the hub ---------------------------------------------------------------


@pytest.mark.anyio
async def test_a_broken_client_is_dropped_not_retried_forever():
    hub = NotificationHub()
    good, bad = FakeClient(), FakeClient(broken=True)
    hub.register(good)
    hub.register(bad)

    delivered = await hub.broadcast({"type": "reminder", "message": "hi"})

    assert delivered == 1  # only the good one counted
    assert hub.client_count == 1  # the dead socket is gone


@pytest.mark.anyio
async def test_broadcasting_to_nobody_is_not_an_error():
    assert await NotificationHub().broadcast({"type": "reminder"}) == 0


def test_registering_the_same_client_twice_does_not_duplicate_it():
    """Otherwise a reconnect would deliver every reminder twice."""
    hub = NotificationHub()
    client = FakeClient()

    hub.register(client)
    hub.register(client)

    assert hub.client_count == 1


# --- relative times (the form that broke live) -----------------------------


@pytest.mark.anyio
async def test_a_relative_reminder_works(db_session, context):
    """
    Regression test for a live failure. Asked to remind "in 40 seconds",
    Claude sent due_at="40 seconds" -- sensible English, not a timestamp.
    Demanding date arithmetic for a relative time was the wrong design.
    """
    result = await CreateReminder().run(
        CreateReminderInput(message="stretch", due_in_seconds=600), context
    )

    assert result.ok
    stored = db_session.scalars(select(Reminder)).one()
    delta = stored.due_at - utcnow_naive()
    assert timedelta(minutes=9) < delta < timedelta(minutes=11)


def test_giving_both_forms_is_refused():
    """Ambiguous input should fail loudly rather than pick one silently."""
    args = CreateReminderInput(
        message="x", due_in_seconds=60, due_at="2026-08-09T21:00:00+05:30"
    )

    with pytest.raises(ToolError, match="not both"):
        args.resolve()


def test_giving_neither_form_is_refused():
    with pytest.raises(ToolError, match="Say when"):
        CreateReminderInput(message="x").resolve()


def test_the_description_renders_a_relative_time_as_a_real_moment():
    """
    So the user approves against an actual clock time, not "in 600 seconds"
    which they would have to work out themselves.
    """
    description = CreateReminder().describe_action(
        CreateReminderInput(message="stretch", due_in_seconds=600)
    )

    assert "stretch" in description
    assert "600" not in description
