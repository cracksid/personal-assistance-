"""
Tests for the settings page and the memory viewer.

The settings half is almost entirely about what must NOT be possible. A
page that can change how the assistant behaves is a page worth being
paranoid about, so the tests that matter are:

  - the API key cannot be read
  - the API key cannot be written
  - nothing outside the allow-list can be written, including settings that
    genuinely exist
  - a bad value is refused rather than stored and blown up on later

The memory half is about the delete actually deleting, in both stores.
"""

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import settings_store
from app.api.deps import get_memory_store
from app.config import settings
from app.db import crud
from app.db.models import Fact, SettingOverride
from app.db.session import get_db
from app.main import app
from app.memory.store import MemoryStore


@pytest.fixture
def client(
    db_session: Session, memory_store: MemoryStore
) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_memory_store] = lambda: memory_store
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def restore_settings() -> Generator[None, None, None]:
    """
    Put the real settings back after each test.

    These tests assign to the process-wide settings object, so without this
    a test that switches the provider would change it for every test after
    it -- and, worse, for the developer's next run.
    """
    saved = {item.key: getattr(settings, item.key) for item in settings_store.EDITABLE}
    yield
    for key, value in saved.items():
        setattr(settings, key, value)


# --- what must not be possible ---------------------------------------------


def test_the_api_key_is_not_readable(client: TestClient):
    """
    THE test for this endpoint. CLAUDE.md: secrets live in .env, are never
    committed, and are never logged. They are not served over HTTP either.
    """
    body = client.get("/settings").json()

    keys = {item["key"] for item in body["settings"]}
    assert "anthropic_api_key" not in keys

    # And no value anywhere in the response looks like a key.
    assert "sk-ant" not in str(body)


def test_the_api_key_cannot_be_written(client: TestClient):
    response = client.patch(
        "/settings", json={"changes": {"anthropic_api_key": "sk-ant-whatever"}}
    )

    assert response.status_code == 400
    assert "not a setting that can be changed" in response.json()["detail"]
    # And the real one is untouched.
    assert settings.anthropic_api_key.get_secret_value() != "sk-ant-whatever"


def test_a_real_setting_that_is_not_on_the_list_is_refused(client: TestClient):
    """
    fs_root exists and is a genuine setting. It is not editable, because
    widening the filesystem sandbox over HTTP is exactly the kind of change
    that should require someone editing .env deliberately.
    """
    before = settings.fs_root

    response = client.patch("/settings", json={"changes": {"fs_root": "C:\\"}})

    assert response.status_code == 400
    assert settings.fs_root == before


def test_a_setting_that_does_not_exist_is_refused(client: TestClient):
    response = client.patch("/settings", json={"changes": {"nonsense": "1"}})

    assert response.status_code == 400
    assert not hasattr(settings, "nonsense")


def test_a_value_of_the_wrong_type_is_refused(client: TestClient, db_session: Session):
    before = settings.agent_max_tool_steps

    response = client.patch(
        "/settings", json={"changes": {"agent_max_tool_steps": "banana"}}
    )

    assert response.status_code == 400
    assert settings.agent_max_tool_steps == before
    # Nothing was stored for a change that did not take.
    assert db_session.scalars(select(SettingOverride)).all() == []


# --- what should work ------------------------------------------------------


def test_listing_shows_current_values(client: TestClient):
    body = client.get("/settings").json()
    by_key = {item["key"]: item for item in body["settings"]}

    assert by_key["llm_provider"]["value"] == settings.llm_provider
    assert by_key["llm_provider"]["kind"] == "choice"
    assert "anthropic" in by_key["llm_provider"]["choices"]


def test_changing_the_provider_takes_effect_immediately(client: TestClient):
    """
    The whole point: switching model should not mean editing .env and
    restarting. get_llm_provider() reads settings each time an Agent is
    built, so the next connection uses the new one.
    """
    client.patch("/settings", json={"changes": {"llm_provider": "ollama"}})

    assert settings.llm_provider == "ollama"


def test_a_change_is_remembered(client: TestClient, db_session: Session):
    client.patch("/settings", json={"changes": {"llm_effort": "high"}})

    stored = db_session.scalars(select(SettingOverride)).one()
    assert (stored.key, stored.value) == ("llm_effort", "high")


def test_changing_the_same_setting_twice_updates_one_row(
    client: TestClient, db_session: Session
):
    client.patch("/settings", json={"changes": {"llm_effort": "high"}})
    client.patch("/settings", json={"changes": {"llm_effort": "low"}})

    stored = db_session.scalars(select(SettingOverride)).all()
    assert len(stored) == 1
    assert stored[0].value == "low"


def test_a_toggle_is_stored_and_read_back_as_a_boolean(client: TestClient):
    client.patch("/settings", json={"changes": {"tools_enabled": "false"}})
    assert settings.tools_enabled is False

    client.patch("/settings", json={"changes": {"tools_enabled": "true"}})
    assert settings.tools_enabled is True


def test_saved_overrides_are_applied_on_startup(db_session: Session):
    """
    .env is the floor and the database is a layer on top, so a change made
    last week is in force before the first connection arrives.
    """
    db_session.add(SettingOverride(key="llm_effort", value="xhigh"))
    db_session.commit()

    assert settings_store.apply_overrides(db_session) == 1
    assert settings.llm_effort == "xhigh"


def test_a_stale_override_does_not_stop_startup(db_session: Session):
    """A setting removed by a later version must not be fatal on boot."""
    db_session.add(SettingOverride(key="a_setting_that_was_removed", value="1"))
    db_session.add(SettingOverride(key="llm_effort", value="low"))
    db_session.commit()

    assert settings_store.apply_overrides(db_session) == 1  # the good one


# --- the memory viewer -----------------------------------------------------


def test_listing_facts_shows_what_is_remembered(
    client: TestClient, db_session: Session, memory_store: MemoryStore
):
    owner = crud.get_or_create_owner(db_session)
    memory_store.remember(db_session, owner.id, "Sid prefers sounddevice", "preference")

    body = client.get("/memory/facts").json()

    assert body["total"] == 1
    assert body["facts"][0]["content"] == "Sid prefers sounddevice"
    assert body["facts"][0]["kind"] == "preference"


def test_forgetting_a_fact_removes_it_from_both_stores(
    client: TestClient, db_session: Session, memory_store: MemoryStore
):
    """
    THE test for the memory viewer. A fact deleted from the table but left
    in the vector index would keep being recalled, which is the exact
    opposite of what the button says it does.
    """
    owner = crud.get_or_create_owner(db_session)
    fact = memory_store.remember(db_session, owner.id, "Sid uses PyAudio", "preference")
    assert memory_store.count() == 1

    response = client.delete(f"/memory/facts/{fact.id}")

    assert response.status_code == 200
    assert db_session.get(Fact, fact.id) is None
    assert memory_store.count() == 0
    assert memory_store.search(owner.id, "PyAudio") == []


def test_forgetting_something_that_does_not_exist_is_a_404(client: TestClient):
    assert client.delete("/memory/facts/9999").status_code == 404


def test_the_listing_reports_the_index_count_too(
    client: TestClient, db_session: Session, memory_store: MemoryStore
):
    """
    Shown next to the total so a mismatch between the source of truth and
    the derived index is visible rather than mysterious.
    """
    owner = crud.get_or_create_owner(db_session)
    memory_store.remember(db_session, owner.id, "Sid plays guitar", "identity")

    body = client.get("/memory/facts").json()

    assert body["total"] == body["indexed"] == 1


def test_rebuilding_the_index_restores_it(
    client: TestClient, db_session: Session, memory_store: MemoryStore
):
    owner = crud.get_or_create_owner(db_session)
    memory_store.remember(db_session, owner.id, "Sid plays guitar", "identity")

    # Simulate a lost index -- the case rebuild exists for.
    memory_store._collection.delete(ids=["1"])
    assert memory_store.count() == 0

    assert client.post("/memory/rebuild").json()["indexed"] == 1
    assert memory_store.count() == 1
