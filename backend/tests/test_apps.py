"""
Tests for the application launcher.

The feature exists because a local model, asked to open File Explorer,
proposed a tool that does not exist:

    {"name": "shell.run", "parameters": {"command": "explorer"}}

So the tests are mostly about the shape of what got built INSTEAD. A tool
that takes a command line accepts every command line; this one takes a name
and looks it up, which is a different kind of thing entirely.
"""

from pathlib import Path

import pytest

from app.tools import apps
from app.tools.apps import ListApps, NoArgs, OpenApp, OpenAppInput, resolve
from app.tools.base import ToolContext


@pytest.fixture
def context(db_session) -> ToolContext:
    return ToolContext(db=db_session)


@pytest.fixture
def launched(monkeypatch) -> list[str]:
    """
    Records what would have been opened, and makes sure nothing is.

    NOTHING IN THE TEST SUITE MAY START A PROGRAM. An early version of these
    tests called run() with a name that resolved, and os.startfile was
    reached for real -- it only failed because a fake .lnk has no
    association. Against a real Start Menu that would have opened an
    application from a test run.
    """
    calls: list[str] = []
    monkeypatch.setattr(apps.os, "startfile", lambda target: calls.append(str(target)))
    return calls


@pytest.fixture
def fake_start_menu(tmp_path, monkeypatch, launched):
    """
    A Start Menu with a few shortcuts in it.

    Real .lnk files are not needed: nothing here opens one, and the
    discovery code only ever looks at names and paths.
    """
    root = tmp_path / "Programs"
    (root / "Accessories").mkdir(parents=True)

    for name in [
        "File Explorer.lnk",
        "Notepad.lnk",
        "Brave.lnk",
        "Visual Studio Code.lnk",
        "Uninstall Brave.lnk",
        "Brave Help.lnk",
    ]:
        (root / name).write_text("", encoding="utf-8")
    (root / "Accessories" / "Character Map.lnk").write_text("", encoding="utf-8")

    monkeypatch.setattr(apps, "START_MENU_ROOTS", (str(root),))
    # The cache is module-level and would otherwise carry the real machine's
    # Start Menu into these tests.
    monkeypatch.setattr(apps, "_cache", {})
    monkeypatch.setattr(apps, "_cached_at", 0.0)
    return root


# --- the design, which is the point ----------------------------------------


def test_open_app_takes_a_name_and_nothing_else():
    """
    THE test for this tool. The model chooses WHICH application; it cannot
    supply a command, arguments, a path, or a working directory.

    If a field is ever added here that carries a command line, the tool has
    become the thing it was built to avoid.
    """
    fields = OpenAppInput.model_fields

    assert set(fields) == {"name"}
    assert fields["name"].annotation is str


def test_opening_an_app_needs_approval():
    """
    Starting a program is visible and real. The gate is what stops a fetched
    web page saying "open the VPN client" from being obeyed silently.
    """
    assert OpenApp().requires_confirmation is True
    assert ListApps().requires_confirmation is False


# --- finding the right one -------------------------------------------------


def test_an_exact_name_is_found(fake_start_menu):
    found = apps.discover_apps(force=True)

    outcome = resolve("File Explorer", found)

    assert isinstance(outcome, tuple)
    assert outcome[0] == "File Explorer"


def test_matching_ignores_case(fake_start_menu):
    outcome = resolve("nOtEpAd", apps.discover_apps(force=True))

    assert isinstance(outcome, tuple)
    assert outcome[0] == "Notepad"


def test_a_partial_name_finds_the_app(fake_start_menu):
    """"open explorer" should find "File Explorer" -- people abbreviate."""
    outcome = resolve("explorer", apps.discover_apps(force=True))

    assert isinstance(outcome, tuple)
    assert outcome[0] == "File Explorer"


def test_an_ambiguous_name_returns_the_candidates(fake_start_menu):
    """
    Handed back rather than guessed at. Opening the wrong program is
    annoying, and picking one silently teaches the model that vague names
    work.
    """
    outcome = resolve("visual", apps.discover_apps(force=True))
    assert isinstance(outcome, tuple)  # only one "visual" here

    (fake_start_menu / "Visual Studio Installer.lnk").write_text("", encoding="utf-8")
    outcome = resolve("visual", apps.discover_apps(force=True))

    assert isinstance(outcome, list)
    assert len(outcome) == 2


@pytest.mark.anyio
async def test_an_unknown_app_says_how_to_find_out(fake_start_menu, context):
    result = await OpenApp().run(OpenAppInput(name="Photoshop"), context)

    assert result.ok is False
    # The message the MODEL reads, so it names the tool that would help.
    assert "list_apps" in result.error


@pytest.mark.anyio
async def test_an_ambiguous_request_lists_what_it_matched(fake_start_menu, context):
    """
    Note "brave" would NOT be ambiguous even with "Brave Beta" installed --
    an exact name wins over a substring, because someone who types the full
    name of a real app means that app. "brav" has no exact match.
    """
    (fake_start_menu / "Brave Beta.lnk").write_text("", encoding="utf-8")
    apps.discover_apps(force=True)

    result = await OpenApp().run(OpenAppInput(name="brav"), context)

    assert result.ok is False
    assert "Brave" in result.error and "Brave Beta" in result.error


@pytest.mark.anyio
async def test_an_exact_name_wins_over_a_partial_one(fake_start_menu, context, launched):
    """Asking for "Brave" opens Brave, even though "Brave Beta" contains it."""
    (fake_start_menu / "Brave Beta.lnk").write_text("", encoding="utf-8")
    apps.discover_apps(force=True)

    result = await OpenApp().run(OpenAppInput(name="Brave"), context)

    assert result.ok
    assert result.output == "Opened Brave."
    assert launched == [str(fake_start_menu / "Brave.lnk")]


# --- what is excluded ------------------------------------------------------


def test_uninstallers_and_help_pages_are_not_launchable(fake_start_menu):
    """
    "Open Brave" must never be able to resolve to "Uninstall Brave". These
    are in the Start Menu but are not applications, and an uninstaller is a
    particularly bad thing to reach by accident.
    """
    found = apps.discover_apps(force=True)

    assert "Brave" in found
    assert "Uninstall Brave" not in found
    assert "Brave Help" not in found


def test_shortcuts_in_subfolders_are_found(fake_start_menu):
    assert "Character Map" in apps.discover_apps(force=True)


# --- the description someone approves --------------------------------------


def test_the_description_names_the_resolved_app_and_its_shortcut(fake_start_menu):
    """
    Approving "open brave" should say what will actually start. A
    description echoing the request back would make the approval nearly
    meaningless.
    """
    apps.discover_apps(force=True)

    description = OpenApp().describe_action(OpenAppInput(name="explorer"))

    assert "File Explorer" in description
    assert ".lnk" in description


def test_the_description_survives_a_name_that_matches_nothing(fake_start_menu):
    """
    describe_action runs BEFORE the gate approves anything, so it must not
    raise on input that run() would reject.
    """
    apps.discover_apps(force=True)

    assert "Photoshop" in OpenApp().describe_action(OpenAppInput(name="Photoshop"))


# --- listing ---------------------------------------------------------------


@pytest.mark.anyio
async def test_listing_shows_the_apps(fake_start_menu, context):
    output = (await ListApps().run(NoArgs(), context)).output

    assert "File Explorer" in output
    assert "Uninstall Brave" not in output


@pytest.mark.anyio
async def test_listing_is_capped(fake_start_menu, context, monkeypatch):
    """This ends up in a prompt, so a few hundred names is real money."""
    for i in range(200):
        (fake_start_menu / f"App {i:03}.lnk").write_text("", encoding="utf-8")
    apps.discover_apps(force=True)

    output = (await ListApps().run(NoArgs(), context)).output

    assert "more" in output
    assert len(output.splitlines()) < 130


# --- the real machine ------------------------------------------------------


def test_discovery_works_against_the_real_start_menu():
    """
    Not a mock. The feature is only worth anything if it finds the programs
    actually installed, and the first thing asked for was File Explorer.
    """
    import sys

    if sys.platform != "win32":
        pytest.skip("Start Menu discovery is Windows-only")

    found = apps.discover_apps(force=True)

    assert found, "no Start Menu shortcuts found at all"
    assert all(isinstance(path, Path) for path in found.values())
