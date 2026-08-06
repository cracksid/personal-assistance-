"""
Tests for the database models.

These check the things that are easy to get wrong and expensive to
discover later: that relationships link rows correctly, that deleting a
conversation cleans up its messages, and that column defaults are what we
think they are.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, Conversation, Message, User
from app.db.session import get_db


def test_user_gets_id_and_defaults_on_commit(db_session):
    user = User(username="sid")

    db_session.add(user)
    db_session.commit()

    # id is assigned by the database, not by us -- it's None until commit.
    assert user.id is not None
    assert user.role == "owner"
    assert user.created_at is not None


def test_conversation_links_messages_in_both_directions(db_session):
    user = User(username="sid")
    conversation = Conversation(title="First chat", user=user)
    conversation.messages.append(Message(role="user", content="hello"))
    conversation.messages.append(Message(role="assistant", content="hi Sid"))

    # Adding the conversation is enough -- SQLAlchemy follows the
    # relationships and saves the user and both messages too.
    db_session.add(conversation)
    db_session.commit()

    assert len(conversation.messages) == 2
    # back_populates means the link works from either side.
    assert conversation.messages[0].conversation is conversation
    assert conversation.user.username == "sid"


def test_deleting_conversation_deletes_its_messages(db_session):
    user = User(username="sid")
    conversation = Conversation(user=user)
    conversation.messages.append(Message(role="user", content="hello"))
    db_session.add(conversation)
    db_session.commit()

    db_session.delete(conversation)
    db_session.commit()

    # cascade="all, delete-orphan" should have removed the message too,
    # rather than leaving it pointing at a conversation that no longer exists.
    remaining = db_session.scalars(select(Message)).all()
    assert remaining == []


def test_audit_log_row_starts_in_started_status(db_session):
    # CLAUDE.md: the audit row is written *before* the tool runs, so a new
    # row must default to "started" -- never to a success value.
    entry = AuditLog(tool_name="delete_file", arguments='{"path": "x.txt"}')

    db_session.add(entry)
    db_session.commit()

    assert entry.status == "started"
    assert entry.requires_confirmation is False
    assert entry.confirmed is False
    assert entry.error_message is None


def test_get_db_yields_a_session_then_cleans_up():
    # get_db is a generator, so next() runs it up to the `yield` and hands
    # back the session. SQLAlchemy connects lazily, so nothing touches the
    # real database file here.
    generator = get_db()
    session = next(generator)

    assert isinstance(session, Session)

    # Exhausting the generator resumes it after the `yield`, which runs
    # the `finally` block that closes the session. StopIteration is the
    # normal signal that a generator has finished.
    with pytest.raises(StopIteration):
        next(generator)
