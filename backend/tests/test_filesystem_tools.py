"""
Tests for what the filesystem tools say when things go wrong.

The happy paths are covered through the gate and the /tools endpoint. What
was not covered is every branch that produces a REFUSAL -- which is the
half that matters most here, for a reason specific to this design:

these messages are read by the model, not just by the user. A tool that
fails with a clear reason lets the model correct itself on the next attempt;
one that fails vaguely makes it guess, and a guessing model with filesystem
tools is exactly what the confirmation gate exists to contain.

Every test uses a real temporary directory as the sandbox rather than
mocking pathlib. The thing under test is behaviour against a real
filesystem -- existence, directories, encodings -- and mocks would assert
my idea of those rather than the operating system's.
"""

import pytest

from app.config import settings
from app.tools.base import ToolContext, ToolError
from app.tools.filesystem import (
    DeleteFile,
    ListDirectory,
    PathInput,
    ReadFile,
    SearchFiles,
    SearchInput,
    WriteFile,
    WriteInput,
)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point FS_ROOT at a throwaway folder for the duration of one test."""
    monkeypatch.setattr(settings, "fs_root", str(tmp_path))
    return tmp_path


@pytest.fixture
def context(db_session) -> ToolContext:
    return ToolContext(db=db_session)


# --- reading ---------------------------------------------------------------


@pytest.mark.anyio
async def test_reading_a_missing_file_names_it(sandbox, context):
    result = await ReadFile().run(PathInput(path="nope.txt"), context)

    assert result.ok is False
    assert "does not exist" in result.error
    assert "nope.txt" in result.error


@pytest.mark.anyio
async def test_reading_a_folder_points_at_the_right_tool(sandbox, context):
    """
    The model asked for something reasonable with the wrong tool. Naming the
    right one is what lets it fix itself without another round trip.
    """
    (sandbox / "papers").mkdir()

    result = await ReadFile().run(PathInput(path="papers"), context)

    assert result.ok is False
    assert "list_directory" in result.error


@pytest.mark.anyio
async def test_a_file_of_invalid_utf8_is_still_readable(sandbox, context):
    """
    errors="replace" rather than raising: one stray byte in a log file
    should not make the whole file unreadable.
    """
    target = sandbox / "log.txt"
    target.write_bytes(b"before \xff\xfe after")

    result = await ReadFile().run(PathInput(path="log.txt"), context)

    assert result.ok
    assert "before" in result.output and "after" in result.output


@pytest.mark.anyio
async def test_a_huge_file_is_truncated_rather_than_loaded_whole(
    sandbox, context, monkeypatch
):
    """Otherwise one read could put a gigabyte into a prompt."""
    monkeypatch.setattr(settings, "fs_max_read_bytes", 50)
    (sandbox / "big.txt").write_text("x" * 500, encoding="utf-8")

    result = await ReadFile().run(PathInput(path="big.txt"), context)

    assert result.ok
    assert "truncated" in result.output
    assert len(result.output) < 200


# --- listing ---------------------------------------------------------------


@pytest.mark.anyio
async def test_listing_something_that_is_not_there(sandbox, context):
    result = await ListDirectory().run(PathInput(path="ghost"), context)

    assert result.ok is False
    assert "does not exist" in result.error


@pytest.mark.anyio
async def test_listing_a_file_says_it_is_a_file(sandbox, context):
    (sandbox / "notes.txt").write_text("hi", encoding="utf-8")

    result = await ListDirectory().run(PathInput(path="notes.txt"), context)

    assert result.ok is False
    assert "is a file" in result.error


@pytest.mark.anyio
async def test_an_empty_folder_says_so_rather_than_returning_nothing(sandbox, context):
    """
    "(empty)" and a blank response look identical to a model otherwise, and
    one of them means "the tool broke".
    """
    (sandbox / "hollow").mkdir()

    result = await ListDirectory().run(PathInput(path="hollow"), context)

    assert result.ok
    assert "(empty)" in result.output


@pytest.mark.anyio
async def test_a_huge_listing_is_capped_and_says_how_many_are_hidden(
    sandbox, context, monkeypatch
):
    monkeypatch.setattr(settings, "fs_max_entries", 5)
    for i in range(20):
        (sandbox / f"file{i:02}.txt").write_text("x", encoding="utf-8")

    result = await ListDirectory().run(PathInput(path="."), context)

    assert result.ok
    assert "and 15 more" in result.output


# --- searching -------------------------------------------------------------


@pytest.mark.anyio
async def test_searching_finds_nothing_without_calling_it_an_error(sandbox, context):
    """
    "Nothing matched" is an answer, not a failure. Returning ok=False would
    make the model retry a search that was perfectly correct.
    """
    result = await SearchFiles().run(SearchInput(pattern="*.rs", path="."), context)

    assert result.ok is True
    assert "Nothing matched" in result.output


@pytest.mark.anyio
async def test_searching_inside_a_file_is_refused(sandbox, context):
    (sandbox / "notes.txt").write_text("hi", encoding="utf-8")

    result = await SearchFiles().run(
        SearchInput(pattern="*", path="notes.txt"), context
    )

    assert result.ok is False
    assert "not a folder" in result.error


@pytest.mark.anyio
async def test_search_finds_files_in_subfolders_with_a_recursive_pattern(
    sandbox, context
):
    (sandbox / "deep").mkdir()
    (sandbox / "deep" / "found.md").write_text("x", encoding="utf-8")

    result = await SearchFiles().run(SearchInput(pattern="**/*.md", path="."), context)

    assert "found.md" in result.output


# --- writing and deleting --------------------------------------------------


@pytest.mark.anyio
async def test_writing_creates_missing_parent_folders(sandbox, context):
    result = await WriteFile().run(
        WriteInput(path="a/b/c/notes.txt", content="hi"), context
    )

    assert result.ok
    assert (sandbox / "a" / "b" / "c" / "notes.txt").read_text(encoding="utf-8") == "hi"


def test_the_write_description_says_when_data_will_be_lost(sandbox):
    """
    The user approves on the strength of this sentence, so overwriting and
    creating must not read the same.
    """
    (sandbox / "existing.txt").write_text("valuable", encoding="utf-8")

    overwrite = WriteFile().describe_action(
        WriteInput(path="existing.txt", content="new")
    )
    create = WriteFile().describe_action(WriteInput(path="brand-new.txt", content="new"))

    assert "OVERWRITE" in overwrite and "will be lost" in overwrite
    assert "Create a new file" in create


def test_the_write_description_survives_a_path_it_would_refuse(sandbox):
    """
    describe_action runs BEFORE the gate approves anything, so it must not
    raise on a path that run() would reject -- the user should see the
    refusal, not a traceback.
    """
    description = WriteFile().describe_action(
        WriteInput(path="C:\\Windows\\System32\\evil.dll", content="x")
    )

    assert "evil.dll" in description


@pytest.mark.anyio
async def test_deleting_something_that_is_not_there(sandbox, context):
    result = await DeleteFile().run(PathInput(path="already-gone.txt"), context)

    assert result.ok is False
    assert "does not exist" in result.error


@pytest.mark.anyio
async def test_deleting_a_folder_is_refused(sandbox, context):
    """
    delete_file deletes one file. Recursive folder deletion is a much
    bigger blast radius than the name promises, and the description the
    user approves says "file".
    """
    (sandbox / "important").mkdir()

    result = await DeleteFile().run(PathInput(path="important"), context)

    assert result.ok is False
    assert (sandbox / "important").exists()


# --- the sandbox still applies to all of them ------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tool, args",
    [
        (ReadFile(), PathInput(path="C:\\Windows\\win.ini")),
        (ListDirectory(), PathInput(path="C:\\Windows")),
        (SearchFiles(), SearchInput(pattern="*", path="C:\\Windows")),
        (DeleteFile(), PathInput(path="C:\\Windows\\win.ini")),
    ],
)
async def test_no_tool_can_reach_outside_the_sandbox(sandbox, context, tool, args):
    """
    Asserted per tool rather than once on safe_resolve, because the
    guarantee is "no tool escapes" and a tool added later that forgets to
    call safe_resolve would still pass a test of safe_resolve alone.
    """
    with pytest.raises(ToolError, match="outside the allowed area"):
        await tool.run(args, context)
