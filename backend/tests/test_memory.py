"""
Tests for the long-term memory store.

The semantic-search test is the important one: it is the whole justification
for taking on Chroma and its 300MB of dependencies. If a keyword search would
have done the job, none of this was worth it.

First run downloads Chroma's embedding model (~80MB) and is slow; later runs
use the cached copy.
"""

import pytest

from app.db import crud
from app.db.models import Fact


@pytest.fixture
def owner_id(db_session) -> int:
    return crud.get_or_create_owner(db_session).id


def test_remember_stores_a_fact(db_session, memory_store, owner_id):
    fact = memory_store.remember(
        db_session, owner_id, "The user's name is Sid", kind="identity"
    )

    assert fact is not None
    assert fact.id is not None

    stored = db_session.query(Fact).all()
    assert [(f.content, f.kind) for f in stored] == [("The user's name is Sid", "identity")]


def test_the_same_fact_is_not_stored_twice(db_session, memory_store, owner_id):
    """
    The extractor re-reports the same fact on every turn, so without this the
    facts table would fill with duplicates within minutes of real use.
    """
    memory_store.remember(db_session, owner_id, "The user's name is Sid")
    second = memory_store.remember(db_session, owner_id, "The user's name is Sid")

    assert second is None
    assert db_session.query(Fact).count() == 1


def test_blank_content_is_ignored(db_session, memory_store, owner_id):
    assert memory_store.remember(db_session, owner_id, "   ") is None
    assert db_session.query(Fact).count() == 0


def test_search_finds_a_fact_by_meaning_not_keywords(db_session, memory_store, owner_id):
    """
    THE test for this phase. The query and the stored fact share no
    meaningful words -- "sound" vs "audio", "what do I use" vs "prefers" --
    so a SQL LIKE or keyword search would return nothing. Semantic search
    finds it because the embeddings are close in meaning.
    """
    memory_store.remember(
        db_session, owner_id, "The user prefers sounddevice over PyAudio", "preference"
    )
    memory_store.remember(db_session, owner_id, "The user's favourite colour is green")

    results = memory_store.search(owner_id, "what do I use for sound?", limit=1)

    assert results == ["The user prefers sounddevice over PyAudio"]


def test_search_on_an_empty_store_returns_nothing(memory_store, owner_id):
    assert memory_store.search(owner_id, "anything at all") == []


def test_rebuild_index_restores_search_after_the_index_is_lost(
    db_session, memory_store, owner_id
):
    """
    Proves the claim that Chroma is derived data: facts written straight to
    SQLite, bypassing the index, become findable again after a rebuild.
    """
    db_session.add(
        Fact(user_id=owner_id, content="The user lives in Bangalore", kind="identity")
    )
    db_session.commit()

    assert memory_store.search(owner_id, "where does the user live?") == []

    count = memory_store.rebuild_index(db_session)

    assert count == 1
    assert memory_store.search(owner_id, "where does the user live?") == [
        "The user lives in Bangalore"
    ]
