"""
Database read/write helpers.

"CRUD" is the usual name for this layer: Create, Read, Update, Delete. Keeping
the queries here rather than inline in core/ means the agent loop reads as a
description of what it does, not as a pile of SQLAlchemy calls -- and if the
schema changes, there is one place to fix.

These functions are synchronous (see the sync-vs-async reasoning in Phase 4).
The async agent loop calls them via asyncio.to_thread so they don't block.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Conversation, Message, User

# JARVIS is single-user (CLAUDE.md). One owner row is created on first use;
# the users table exists so multi-user stays possible without a migration.
OWNER_USERNAME = "owner"

# How many past messages to send to the model. Every message costs tokens, so
# this is a deliberate cap rather than "the whole history". Phase 6 replaces
# this crude window with real memory: summaries plus retrieved facts.
DEFAULT_HISTORY_LIMIT = 20


def get_or_create_owner(db: Session) -> User:
    """Return the single owner user, creating it on first run."""
    owner = db.scalars(select(User).where(User.username == OWNER_USERNAME)).first()
    if owner is None:
        owner = User(username=OWNER_USERNAME)
        db.add(owner)
        db.commit()
    return owner


def create_conversation(db: Session, user: User) -> Conversation:
    """Start a new conversation belonging to `user`."""
    conversation = Conversation(user_id=user.id)
    db.add(conversation)
    db.commit()
    return conversation


def get_or_create_active_conversation(db: Session, user: User) -> Conversation:
    """
    Resume the most recent conversation, or start one if there is none.

    This is what makes short-term memory survive closing the tab: before
    Phase 6 every connection created a fresh conversation, so reconnecting
    meant the assistant had forgotten the last thing you said.

    Long-term memory (the facts table) is separate and survives regardless --
    it is not tied to any conversation.
    """
    latest = db.scalars(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.id.desc())
        .limit(1)
    ).first()
    if latest is not None:
        return latest
    return create_conversation(db, user)


def get_last_exchange(db: Session, conversation_id: int) -> tuple[str, str] | None:
    """
    Return the most recent (user message, assistant reply) pair.

    Returns None unless the last two messages are exactly that pair -- so a
    turn that failed partway through is never fed to the fact extractor.
    """
    recent = get_recent_messages(db, conversation_id, limit=2)
    if len(recent) == 2 and recent[0].role == "user" and recent[1].role == "assistant":
        return recent[0].content, recent[1].content
    return None


def add_message(db: Session, conversation_id: int, role: str, content: str) -> Message:
    """Append one message to a conversation."""
    message = Message(conversation_id=conversation_id, role=role, content=content)
    db.add(message)
    db.commit()
    return message


def get_recent_messages(
    db: Session, conversation_id: int, limit: int = DEFAULT_HISTORY_LIMIT
) -> list[Message]:
    """
    Return the last `limit` messages of a conversation, oldest first.

    The query sorts newest-first so LIMIT keeps the most RECENT messages, then
    the result is reversed -- because the model needs them in chronological
    order. Sorting oldest-first and taking LIMIT would keep the oldest
    messages and throw away everything the user just said.
    """
    newest_first = db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id.desc())
        .limit(limit)
    ).all()
    return list(reversed(newest_first))
