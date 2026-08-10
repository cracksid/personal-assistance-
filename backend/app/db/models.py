"""
Database models -- one Python class per table.

Every class here inherits from Base, which is what marks it as a table.
An instance of a class is one row; each Mapped[...] attribute is a column.

Style note: this is SQLAlchemy 2.0's typed declarative style. The type
hint on each attribute (Mapped[int], Mapped[str | None], ...) is not just
documentation -- SQLAlchemy reads it to decide the column's type and
whether it is allowed to be NULL. `Mapped[str]` is NOT NULL;
`Mapped[str | None]` is nullable.
"""

from datetime import datetime, timezone

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    """
    Timestamps are stored in UTC, never local time. Local timestamps are
    ambiguous across daylight-saving changes and meaningless if the data
    ever moves between machines.

    Gotcha worth knowing: this returns a timezone-*aware* datetime, but
    SQLite has no real datetime type and gives values back timezone-naive.
    So a value read from the database won't compare equal to the one that
    was written. The stored instant is still correct and still UTC -- only
    the tzinfo tag is lost.
    """
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    # primary_key=True makes this the row's unique identifier. SQLite fills
    # it in automatically (1, 2, 3, ...) so we never set it by hand.
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)

    # Single user today (see CLAUDE.md: no login system is being built).
    # The column exists so multi-user is a data change later, not a schema
    # migration plus a rewrite.
    role: Mapped[str] = mapped_column(String(20), default="owner")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    facts: Mapped[list["Fact"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    reminders: Mapped[list["Reminder"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    scheduled_tasks: Mapped[list["ScheduledTask"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)

    # ForeignKey("users.id") is the actual database-level link: this column
    # holds the id of a row in the users table.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str | None] = mapped_column(String(200), default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    # relationship() is the Python-side convenience built on that foreign
    # key -- it lets us write conversation.messages instead of writing a
    # JOIN by hand.
    #
    # back_populates names the matching attribute on the other class, so
    # the two stay in sync in memory: appending to conversation.messages
    # also sets message.conversation.
    #
    # cascade="all, delete-orphan" means deleting a conversation deletes
    # its messages too, instead of leaving orphaned rows pointing at a
    # conversation that no longer exists.
    user: Mapped["User"] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))

    # "user", "assistant", "system", or "tool" -- the standard roles in a
    # chat completion. Kept as a plain string rather than a database enum:
    # SQLite has no real enum type, and new roles shouldn't need a migration.
    role: Mapped[str] = mapped_column(String(20))

    # Text (not String) because message bodies have no sensible length limit.
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")


class Fact(Base):
    """
    One durable thing worth remembering about the user.

    Facts are long-term memory: they outlive the conversation they were
    learned in, which is what separates them from Messages. "Sid prefers
    sounddevice over PyAudio" stays true next week; "what's the weather?"
    does not.

    This table is the source of truth. The Chroma vector index is a derived
    search structure built from these rows and can be rebuilt from them.
    """

    __tablename__ = "facts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))

    # The fact itself, written as a plain sentence -- that's what gets
    # embedded for semantic search and what gets shown to the model.
    content: Mapped[str] = mapped_column(Text)

    # Rough category: "identity", "preference", "project", or "other".
    # A plain string rather than a database enum, for the same reason as
    # Message.role -- SQLite has no real enum, and new kinds shouldn't
    # need a migration.
    kind: Mapped[str] = mapped_column(String(30), default="other")

    # Provenance: which conversation this was learned from. Nullable because
    # a fact may be entered directly, and because the conversation may later
    # be deleted while the fact remains true.
    source_conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    user: Mapped["User"] = relationship(back_populates="facts")


class Reminder(Base):
    """
    Something to tell the user at a particular moment.

    Stored in the database rather than only in the scheduler's memory, so a
    reminder survives a restart. APScheduler is asked to check this table on
    a timer; it is not asked to remember anything itself.

    That choice matters: an in-memory job vanishes when the process stops,
    which for a desktop assistant is every time the machine sleeps. A row
    outlives all of that.
    """

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))

    # What to say when the time comes.
    message: Mapped[str] = mapped_column(Text)

    # When to say it. Stored in UTC like every other timestamp here -- the
    # model converts the user's "9pm tonight" into an absolute instant
    # before it ever reaches this table.
    due_at: Mapped[datetime] = mapped_column()

    # "pending" -> "delivered", or "cancelled".
    #
    # Delivery is only marked once a client has actually received it. A
    # reminder that came due while JARVIS was closed stays pending and is
    # delivered when you next connect, rather than being silently lost.
    status: Mapped[str] = mapped_column(String(20), default="pending")

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(default=None)

    user: Mapped["User"] = relationship(back_populates="reminders")


class ScheduledTask(Base):
    """
    A prompt JARVIS runs to itself, on a repeating schedule.

    A Reminder carries text to read out. A ScheduledTask carries an
    INSTRUCTION -- "summarise my pending reminders" -- which is run through
    the agent loop, tools and all, and whose *answer* is what gets pushed to
    the user. That is the whole difference, and it is why this one needs the
    unattended rule in core/gate.py and a Reminder does not.

    WHY THE SCHEDULE IS STORED AS A LOCAL "HH:MM" AND NOT A UTC INSTANT.

    Every other timestamp here is UTC, for good reasons. A daily time is the
    exception, because "8am" is not an instant -- it is a recurring position
    in the user's day. Convert it to UTC once and store that, and it drifts
    to 7am or 9am the next time the clocks change. Storing the wall-clock
    time the user asked for and converting on each run keeps 8am at 8am.

    next_run_at IS a UTC instant, because that is a specific moment and the
    scheduler compares it against now. It is recomputed after every run.
    """

    __tablename__ = "scheduled_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))

    # A short handle, so the user can say "cancel the morning briefing".
    name: Mapped[str] = mapped_column(String(100))

    # What to ask JARVIS when it fires, phrased as an instruction.
    prompt: Mapped[str] = mapped_column(Text)

    # "interval" (every N seconds) or "daily" (at a wall-clock time).
    schedule_kind: Mapped[str] = mapped_column(String(20), default="interval")
    interval_seconds: Mapped[int | None] = mapped_column(default=None)
    daily_at: Mapped[str | None] = mapped_column(String(5), default=None)  # "08:00"

    # Naive UTC, like Reminder.due_at, for the same SQLite reason.
    next_run_at: Mapped[datetime] = mapped_column()
    last_run_at: Mapped[datetime | None] = mapped_column(default=None)

    # "active" or "cancelled". Cancelled rows are kept rather than deleted so
    # the history of what ran, and what it said, stays readable.
    status: Mapped[str] = mapped_column(String(20), default="active")

    # Each task keeps its own conversation, so a daily briefing builds up
    # context across days without mixing into the user's live chat.
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id"), default=None
    )

    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    user: Mapped["User"] = relationship(back_populates="scheduled_tasks")


class AuditLog(Base):
    """
    One row per tool execution.

    CLAUDE.md: "An audit log row is written *before* any tool executes,
    not after." That is why `status` exists -- the row is inserted with
    status="started" before the tool runs, then updated to "success" or
    "error" afterwards. A crash mid-execution therefore leaves a visible
    "started" row rather than no evidence at all.

    No tools exist yet (Phase 9). This is the table only.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Nullable: a scheduled/automated action (Phase 11) has no user behind it.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)

    tool_name: Mapped[str] = mapped_column(String(100))

    # The tool's input, stored as a JSON string. Kept as text rather than
    # separate columns because every tool has a different input shape.
    arguments: Mapped[str] = mapped_column(Text, default="{}")

    # Whether the tool declared requires_confirmation, and whether the user
    # actually confirmed. Recorded together so the log can answer "did
    # anything sensitive run without approval?"
    requires_confirmation: Mapped[bool] = mapped_column(default=False)
    confirmed: Mapped[bool] = mapped_column(default=False)

    status: Mapped[str] = mapped_column(String(20), default="started")
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
