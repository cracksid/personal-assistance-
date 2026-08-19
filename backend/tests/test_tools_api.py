"""
End-to-end tests for the /tools endpoints.

Unlike test_gate.py, these use the REAL filesystem tools and the REAL gate,
against a throwaway sandbox in a temporary directory. They answer the
question the unit tests cannot: does the whole thing, wired together as the
user will actually call it, refuse to do the dangerous thing?
"""

from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_gate
from app.config import settings
from app.core.gate import ToolGate
from app.db.session import get_db
from app.main import app


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch) -> Path:
    """A temporary sandbox, so no test can touch the real home directory."""
    monkeypatch.setattr(settings, "fs_root", str(tmp_path))
    (tmp_path / "notes.txt").write_text("guitar practice at nine")
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "readme.md").write_text("# hello")
    return tmp_path.resolve()


@pytest.fixture
def client(
    sandbox: Path, db_session: Session
) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    # ONE gate per test, not one per request. The gate holds pending
    # confirmations in memory, so `lambda: ToolGate()` would hand out a
    # fresh empty gate to every call -- and the confirm request would never
    # find what the invoke request stored. That is exactly why deps.py keeps
    # a module-level singleton.
    gate = ToolGate()
    app.dependency_overrides[get_gate] = lambda: gate
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def invoke(client: TestClient, name: str, **arguments):
    return client.post(f"/tools/{name}/invoke", json=arguments)


# --- discovery -------------------------------------------------------------


def test_listing_tools_shows_which_ones_are_dangerous(client: TestClient):
    """
    An EXACT set, not a subset, on purpose. This is the inventory of
    everything that can happen without being asked twice, so adding a tool
    that needs approval should require someone to come here and say so
    deliberately -- a failing test is the right way to be told.
    """
    body = client.get("/tools").json()

    dangerous = {t["name"] for t in body["tools"] if t["requires_confirmation"]}
    assert dangerous == {
        "write_file",
        "delete_file",
        "create_scheduled_task",
        "open_app",
    }
    # The caller is told where the boundary is, not left to guess.
    assert body["sandbox_root"]


def test_listing_includes_a_schema_for_each_tool(client: TestClient):
    """Phase 9b hands these schemas to the model so it knows how to call them."""
    body = client.get("/tools").json()

    read_file = next(t for t in body["tools"] if t["name"] == "read_file")
    assert "path" in read_file["input_schema"]["properties"]


# --- read-only tools -------------------------------------------------------


def test_read_file_returns_contents(client: TestClient):
    response = invoke(client, "read_file", path="notes.txt")

    assert response.json()["result"]["output"] == "guitar practice at nine"
    # Read-only tools run immediately -- no confirmation involved.
    assert response.json()["confirmation"] is None


def test_list_directory_shows_files_and_folders(client: TestClient):
    output = invoke(client, "list_directory", path=".").json()["result"]["output"]

    assert "notes.txt" in output
    assert "[dir] projects" in output


def test_search_files_finds_nested_matches(client: TestClient):
    output = invoke(client, "search_files", pattern="**/*.md", path=".").json()[
        "result"
    ]["output"]

    assert "readme.md" in output


def test_reading_a_missing_file_fails_gracefully(client: TestClient):
    result = invoke(client, "read_file", path="nope.txt").json()["result"]

    assert result["ok"] is False
    assert "does not exist" in result["error"]


# --- the confirmation flow, end to end -------------------------------------


def test_write_file_asks_before_touching_anything(client: TestClient, sandbox: Path):
    """
    The whole point of the phase: a destructive call over the real API
    changes nothing until a human says yes.
    """
    body = invoke(client, "write_file", path="new.txt", content="hello").json()

    assert body["result"] is None
    assert body["confirmation"]["tool_name"] == "write_file"
    assert not (sandbox / "new.txt").exists()  # nothing was written


def test_confirming_a_write_actually_writes(client: TestClient, sandbox: Path):
    body = invoke(client, "write_file", path="new.txt", content="hello").json()

    result = client.post(
        "/tools/confirm",
        json={"confirmation_id": body["confirmation"]["confirmation_id"]},
    ).json()

    assert result["ok"] is True
    assert (sandbox / "new.txt").read_text() == "hello"


def test_the_description_warns_that_an_overwrite_destroys_data(client: TestClient):
    """
    A vague description makes approval meaningless. The user must be told
    that an existing file will be lost, not merely that a file is "written".
    """
    body = invoke(client, "write_file", path="notes.txt", content="new").json()

    description = body["confirmation"]["description"]
    assert "OVERWRITE" in description
    assert "will be lost" in description


def test_cancelling_a_delete_leaves_the_file_alone(client: TestClient, sandbox: Path):
    body = invoke(client, "delete_file", path="notes.txt").json()

    client.post(
        "/tools/cancel",
        json={"confirmation_id": body["confirmation"]["confirmation_id"]},
    )

    assert (sandbox / "notes.txt").exists()


def test_confirming_a_delete_removes_the_file(client: TestClient, sandbox: Path):
    body = invoke(client, "delete_file", path="notes.txt").json()

    client.post(
        "/tools/confirm",
        json={"confirmation_id": body["confirmation"]["confirmation_id"]},
    )

    assert not (sandbox / "notes.txt").exists()


def test_delete_refuses_folders(client: TestClient, sandbox: Path):
    """
    Recursive directory deletion is the most destructive thing a tool could
    do, so it is not offered at all -- not even behind a confirmation.
    """
    body = invoke(client, "delete_file", path="projects").json()
    result = client.post(
        "/tools/confirm",
        json={"confirmation_id": body["confirmation"]["confirmation_id"]},
    ).json()

    assert result["ok"] is False
    assert "only deletes single files" in result["error"]
    assert (sandbox / "projects").exists()


# --- the sandbox holds through the API -------------------------------------


def test_reading_outside_the_sandbox_is_refused(client: TestClient):
    result = invoke(client, "read_file", path="C:\\Windows\\win.ini").json()["result"]

    assert result["ok"] is False
    assert "outside the allowed area" in result["error"]


def test_traversal_through_the_api_is_refused(client: TestClient):
    result = invoke(client, "read_file", path="../../../etc/passwd").json()["result"]

    assert result["ok"] is False
    assert "outside the allowed area" in result["error"]


def test_writing_outside_the_sandbox_is_refused_even_if_confirmed(
    client: TestClient,
):
    """
    Confirmation approves the ACTION, not the location. Saying yes must not
    grant an escape from the sandbox.
    """
    body = invoke(client, "write_file", path="C:\\Windows\\evil.txt", content="x").json()
    result = client.post(
        "/tools/confirm",
        json={"confirmation_id": body["confirmation"]["confirmation_id"]},
    ).json()

    assert result["ok"] is False
    assert "outside the allowed area" in result["error"]


def test_the_env_file_cannot_be_read_even_inside_the_sandbox(
    client: TestClient, sandbox: Path
):
    """JARVIS must not be able to read its own API keys."""
    (sandbox / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-secret")

    result = invoke(client, "read_file", path=".env").json()["result"]

    assert result["ok"] is False
    assert "credentials" in result["error"]


def test_search_does_not_leak_denied_files(client: TestClient, sandbox: Path):
    """
    A glob can match things the caller could not have read directly, so
    results are filtered as well as the starting folder.
    """
    (sandbox / ".env").write_text("secret")

    output = invoke(client, "search_files", pattern="*", path=".").json()["result"][
        "output"
    ]

    assert ".env" not in output
