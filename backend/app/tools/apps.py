"""
Launching applications.

WHY THIS TOOL TAKES AN APP NAME AND NOT A COMMAND.

The obvious version of this feature is a `run_command` tool: hand it a
string, run it. A local model asked to open File Explorer proposed exactly
that, unprompted:

    {"name": "shell.run", "parameters": {"command": "explorer"}}

That shape is indefensible. A tool that accepts a command line accepts
every command line, and the confirmation gate cannot save it: the human is
approving a sentence, and "run: explorer" and
"run: explorer & del /s /q C:\\Users" look equally reasonable at a glance.
Worse, since Phase 10 the model reads pages written by strangers, so the
string it proposes is not always its own idea.

So this tool does something narrower on purpose. It scans the Start Menu --
a list of programs someone already chose to install -- and the model
chooses WHICH ONE. It cannot invent an entry, cannot pass arguments, and
cannot reach a program that is not in the list.

The model picks from a menu. It does not write the order.

NO SHELL, ANYWHERE.

os.startfile hands a resolved path to the Windows file association
machinery. There is no command string to parse and therefore nothing to
inject into: no quoting bug, no `&`, no `|`. subprocess with a list is used
elsewhere for the same reason.
"""

import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from pydantic import BaseModel, Field

from app.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

# The two places Windows keeps Start Menu shortcuts: one for all users, one
# for this user.
START_MENU_ROOTS = (
    r"%ProgramData%\Microsoft\Windows\Start Menu\Programs",
    r"%APPDATA%\Microsoft\Windows\Start Menu\Programs",
)

# Entries that are documentation, uninstallers or setup wizards rather than
# applications. Launching one of these is never what "open X" meant, and an
# uninstaller in particular is a bad thing to reach by accident.
IGNORED_WORDS = (
    "uninstall",
    "remove ",
    " help",
    "help ",
    "readme",
    "release notes",
    "documentation",
    "manual",
    "website",
    "web site",
    "homepage",
)

# Scanning a few hundred shortcuts takes a moment, and the Start Menu does
# not change between one sentence and the next.
CACHE_SECONDS = 300

_cache: dict[str, Path] = {}
_cached_at = 0.0


def discover_apps(force: bool = False) -> dict[str, Path]:
    """
    Every launchable Start Menu entry, as {display name: shortcut path}.

    Cached, because this walks a few hundred files and the answer is stable.
    """
    global _cache, _cached_at

    if _cache and not force and time.monotonic() - _cached_at < CACHE_SECONDS:
        return _cache

    found: dict[str, Path] = {}
    for template in START_MENU_ROOTS:
        root = Path(os.path.expandvars(template))
        if not root.is_dir():
            continue
        try:
            for shortcut in root.rglob("*.lnk"):
                name = shortcut.stem
                lowered = name.lower()
                if any(word in lowered for word in IGNORED_WORDS):
                    continue
                # First one wins, so an all-users entry is not replaced by a
                # per-user duplicate of the same name.
                found.setdefault(name, shortcut)
        except OSError as exc:
            logger.warning("Could not read %s: %s", root, exc)

    _cache = found
    _cached_at = time.monotonic()
    logger.info("Discovered %s launchable applications", len(found))
    return found


def resolve(name: str, apps: dict[str, Path]) -> tuple[str, Path] | list[str]:
    """
    Find the app someone meant.

    Returns the single match, or a list of candidates when the request is
    ambiguous. Ambiguity is handed back rather than guessed at: opening the
    wrong program is annoying, and picking one silently would teach the
    model that vague names work.
    """
    wanted = name.strip().lower()
    if not wanted:
        return []

    for display in apps:
        if display.lower() == wanted:
            return display, apps[display]

    # "explorer" should find "File Explorer", "vs code" should find
    # "Visual Studio Code".
    matches = [display for display in apps if wanted in display.lower()]
    if len(matches) == 1:
        return matches[0], apps[matches[0]]
    return sorted(matches)


class OpenAppInput(BaseModel):
    name: str = Field(
        description=(
            "The application name as it appears in the Start Menu, e.g. "
            "'File Explorer', 'Notepad', 'Brave'. Call list_apps first if "
            "you are unsure what is installed. This is a name, not a "
            "command line: arguments cannot be passed."
        )
    )


class OpenApp(Tool):
    name = "open_app"
    description = (
        "Open an installed application by name, e.g. 'File Explorer' or "
        "'Notepad'. Only programs in the Start Menu can be opened, and no "
        "arguments can be passed to them. Use list_apps to see what is "
        "available."
    )
    input_schema = OpenAppInput

    # Starting a program is a visible, real-world action, and the gate is
    # what keeps a fetched web page saying "open the VPN client" from being
    # obeyed silently.
    requires_confirmation = True

    def describe_action(self, args: OpenAppInput) -> str:
        # Names the RESOLVED entry rather than what was asked for. Approving
        # "open brave" should say it will start "Brave" from a specific
        # shortcut, otherwise the approval means very little.
        outcome = resolve(args.name, discover_apps())
        if isinstance(outcome, tuple):
            display, path = outcome
            return f"Open the application {display!r} ({path})"
        return f"Open the application {args.name!r}"

    async def run(self, args: OpenAppInput, context: ToolContext) -> ToolResult:
        apps = await asyncio.to_thread(discover_apps)
        outcome = resolve(args.name, apps)

        if isinstance(outcome, list):
            if not outcome:
                return ToolResult(
                    ok=False,
                    error=(
                        f"No installed application matches {args.name!r}. "
                        "Use list_apps to see what is available."
                    ),
                )
            return ToolResult(
                ok=False,
                error=(
                    f"{args.name!r} matches several applications: "
                    f"{', '.join(outcome[:8])}. Ask for one exactly."
                ),
            )

        display, path = outcome

        def launch() -> None:
            if sys.platform == "win32":
                # A path and an OS association -- no command string exists
                # here, so there is nothing to inject into.
                os.startfile(path)
            else:
                # A list, never a string: the same property elsewhere.
                subprocess.Popen(["xdg-open", str(path)])

        try:
            await asyncio.to_thread(launch)
        except OSError as exc:
            return ToolResult(ok=False, error=f"Could not open {display}: {exc}")

        logger.info("Opened application %s (%s)", display, path)
        return ToolResult(output=f"Opened {display}.")


class NoArgs(BaseModel):
    pass


class ListApps(Tool):
    name = "list_apps"
    description = (
        "List the applications that can be opened. Use this when the user "
        "asks what can be opened, or before open_app when unsure of the "
        "exact name."
    )
    input_schema = NoArgs
    requires_confirmation = False

    def describe_action(self, args: NoArgs) -> str:
        return "List installed applications"

    async def run(self, args: NoArgs, context: ToolContext) -> ToolResult:
        apps = await asyncio.to_thread(discover_apps)
        if not apps:
            return ToolResult(output="No Start Menu applications were found.")

        names = sorted(apps)
        # Capped for the same reason every other listing is: this ends up in
        # a prompt, and a couple of hundred names is a lot of tokens to
        # spend on a menu.
        shown = names[:120]
        body = "\n".join(shown)
        if len(names) > len(shown):
            body += f"\n... and {len(names) - len(shown)} more"
        return ToolResult(output=body)


def build_app_tools() -> list[Tool]:
    return [OpenApp(), ListApps()]
