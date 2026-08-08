"""
Tests for the path sandbox.

These are the most important tests in the project. Everything else being
correct does not matter if a tool can be talked into writing outside the
sandbox, so most of what follows is deliberate attempts to escape it.

The rule under test: a path is safe only if, AFTER resolving `..` and
following symlinks, it still lands inside FS_ROOT and is not deny-listed.
"""

import sys
from pathlib import Path

import pytest

from app.config import settings
from app.tools.base import ToolError
from app.tools.paths import safe_resolve, sandbox_root


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch) -> Path:
    """A throwaway sandbox, so tests never touch the real home directory."""
    monkeypatch.setattr(settings, "fs_root", str(tmp_path))
    (tmp_path / "notes.txt").write_text("hello")
    (tmp_path / "projects").mkdir()
    return tmp_path.resolve()


# --- what should be allowed ------------------------------------------------


def test_a_file_inside_the_sandbox_is_allowed(sandbox: Path):
    assert safe_resolve(str(sandbox / "notes.txt")) == sandbox / "notes.txt"


def test_a_relative_path_resolves_against_the_sandbox_not_the_cwd(sandbox: Path):
    """
    The caller cannot see the server's working directory, and it changes
    depending on how the server was started -- so relative paths must be
    anchored to something predictable.
    """
    assert safe_resolve("notes.txt") == sandbox / "notes.txt"


def test_a_path_that_does_not_exist_yet_is_allowed(sandbox: Path):
    """write_file has to validate its destination before creating it."""
    assert safe_resolve(str(sandbox / "new" / "file.txt")) == sandbox / "new" / "file.txt"


# --- escape attempts -------------------------------------------------------


def test_an_absolute_path_outside_the_sandbox_is_refused(sandbox: Path):
    with pytest.raises(ToolError, match="outside the allowed area"):
        safe_resolve("C:\\Windows\\System32\\drivers\\etc\\hosts")


def test_dot_dot_traversal_is_refused(sandbox: Path):
    """
    The classic escape. It only fails if resolution happens BEFORE the
    containment check -- as a raw string this starts with the sandbox path
    and would pass a naive prefix test.
    """
    with pytest.raises(ToolError, match="outside the allowed area"):
        safe_resolve(str(sandbox / ".." / ".." / "Windows"))


def test_deeply_nested_traversal_is_refused(sandbox: Path):
    with pytest.raises(ToolError, match="outside the allowed area"):
        safe_resolve("projects/../../../../../../etc/passwd")


def test_the_parent_of_the_sandbox_is_refused(sandbox: Path):
    with pytest.raises(ToolError, match="outside the allowed area"):
        safe_resolve(str(sandbox.parent))


@pytest.mark.skipif(
    sys.platform == "win32", reason="creating symlinks on Windows needs admin rights"
)
def test_a_symlink_pointing_out_of_the_sandbox_is_refused(sandbox: Path, tmp_path: Path):
    """
    Why resolution must follow symlinks, not just collapse `..`. The link
    itself lives inside the sandbox; only its target escapes.
    """
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("secret")
    link = sandbox / "innocent.txt"
    link.symlink_to(outside)

    with pytest.raises(ToolError, match="outside the allowed area"):
        safe_resolve(str(link))


def test_an_empty_path_is_refused(sandbox: Path):
    with pytest.raises(ToolError, match="No path"):
        safe_resolve("   ")


# --- the deny-list ---------------------------------------------------------


@pytest.mark.parametrize(
    "path,why",
    [
        (".env", "JARVIS's own API keys live here"),
        (".ssh/id_rsa", "private keys"),
        (".aws/credentials", "cloud credentials"),
        ("projects/.env", "a project's secrets, nested"),
        (".gnupg/secring.gpg", "GPG keys"),
    ],
)
def test_sensitive_paths_inside_the_sandbox_are_still_refused(
    sandbox: Path, path: str, why: str
):
    """
    Containment alone is not enough. A home directory is full of things a
    tool has no business reading, including this application's own key.
    """
    with pytest.raises(ToolError, match="credentials or"):
        safe_resolve(path)


def test_the_deny_list_is_case_insensitive(sandbox: Path):
    """
    Windows filesystems are case-insensitive, so ~/.SSH and ~/.ssh are the
    same directory. A case-sensitive deny-list would be trivially bypassed.
    """
    with pytest.raises(ToolError, match="credentials or"):
        safe_resolve(".SSH/id_rsa")


def test_a_normal_file_is_not_caught_by_the_deny_list(sandbox: Path):
    """The deny-list must not be so broad it blocks ordinary work."""
    assert safe_resolve("projects/environment.md").name == "environment.md"


# --- configuration ---------------------------------------------------------


def test_sandbox_root_follows_config(sandbox: Path):
    assert sandbox_root() == sandbox
