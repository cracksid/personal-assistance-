"""
Tools for watching folders.

These only write rows. The watcher service reads them back on a timer and
makes the running watches match -- the tools never reach into it directly,
so the database stays the only state and a restart needs no recovery code.

Note what is NOT here: any tool that reacts to a file. A watcher reports
that something changed and stops. Reading the file, summarising it, or
moving it is a request the user makes, with the confirmation gate in front
of it as usual.
"""

import asyncio
import logging

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.config import settings
from app.db.models import WatchedFolder
from app.tools.base import Tool, ToolContext, ToolError, ToolResult
from app.tools.paths import safe_resolve

logger = logging.getLogger(__name__)


class WatchInput(BaseModel):
    path: str = Field(description="The folder to watch.")
    recursive: bool = Field(
        default=False,
        description=(
            "Watch subfolders too. Leave false unless asked -- a project "
            "tree can produce thousands of events from one build."
        ),
    )


class WatchFolder(Tool):
    name = "watch_folder"
    description = (
        "Tell the user when files change in a folder. JARVIS will report "
        "what changed and nothing more -- it does not read, move or act on "
        "the files."
    )
    input_schema = WatchInput

    # Additive, reversible, and it cannot act on anything. Like a reminder,
    # it is audited but does not interrupt to ask.
    requires_confirmation = False

    def describe_action(self, args: WatchInput) -> str:
        scope = " and its subfolders" if args.recursive else ""
        return f"Report changes in {args.path}{scope}"

    async def run(self, args: WatchInput, context: ToolContext) -> ToolResult:
        if context.user_id is None:
            return ToolResult(ok=False, error="No user to watch a folder for.")

        # The same sandbox check every filesystem tool uses. Resolution
        # happens before the check, so '..' and symlinks cannot escape.
        try:
            target = safe_resolve(args.path)
        except ToolError as exc:
            return ToolResult(ok=False, error=str(exc))

        if not target.is_dir():
            return ToolResult(
                ok=False, error=f"{target} is not a folder, so there is nothing to watch."
            )

        def save() -> str:
            existing = context.db.scalars(
                select(WatchedFolder).where(
                    WatchedFolder.user_id == context.user_id,
                    WatchedFolder.path == str(target),
                    WatchedFolder.status == "active",
                )
            ).first()
            if existing is not None:
                return f"already:{existing.id}"

            active = context.db.scalar(
                select(func.count())
                .select_from(WatchedFolder)
                .where(
                    WatchedFolder.user_id == context.user_id,
                    WatchedFolder.status == "active",
                )
            )
            if active >= settings.watch_max_folders:
                return "limit"

            watch = WatchedFolder(
                user_id=context.user_id,
                path=str(target),
                recursive=args.recursive,
            )
            context.db.add(watch)
            context.db.commit()
            return f"created:{watch.id}"

        outcome = await asyncio.to_thread(save)

        if outcome == "limit":
            return ToolResult(
                ok=False,
                error=(
                    f"Already watching {settings.watch_max_folders} folders, "
                    "which is the limit. Stop watching one first."
                ),
            )
        if outcome.startswith("already:"):
            return ToolResult(
                output=f"Already watching {target} (watch #{outcome.split(':')[1]})."
            )

        watch_id = outcome.split(":")[1]
        logger.info("Watching folder %s (watch %s)", target, watch_id)
        scope = ", including subfolders" if args.recursive else ""
        return ToolResult(
            output=(
                f"Watch #{watch_id} added: {target}{scope}. Changes will be "
                "reported as they happen, once JARVIS is connected."
            )
        )


class NoArgs(BaseModel):
    pass


class ListWatchedFolders(Tool):
    name = "list_watched_folders"
    description = "Show which folders are being watched for changes."
    input_schema = NoArgs
    requires_confirmation = False

    def describe_action(self, args: NoArgs) -> str:
        return "List watched folders"

    async def run(self, args: NoArgs, context: ToolContext) -> ToolResult:
        if context.user_id is None:
            return ToolResult(ok=False, error="No user to list watches for.")

        def load() -> list[tuple[int, str, bool]]:
            rows = context.db.scalars(
                select(WatchedFolder)
                .where(
                    WatchedFolder.user_id == context.user_id,
                    WatchedFolder.status == "active",
                )
                .order_by(WatchedFolder.id)
            ).all()
            return [(r.id, r.path, r.recursive) for r in rows]

        watches = await asyncio.to_thread(load)
        if not watches:
            return ToolResult(output="No folders are being watched.")

        lines = [
            f"#{wid}  {path}{'  (including subfolders)' if rec else ''}"
            for wid, path, rec in watches
        ]
        return ToolResult(output="\n".join(lines))


class UnwatchInput(BaseModel):
    watch_id: int = Field(description="The number shown by list_watched_folders.")


class UnwatchFolder(Tool):
    name = "unwatch_folder"
    description = (
        "Stop reporting changes in a folder, by its number. Call "
        "list_watched_folders first if you do not know the number."
    )
    input_schema = UnwatchInput
    requires_confirmation = False

    def describe_action(self, args: UnwatchInput) -> str:
        return f"Stop watching folder #{args.watch_id}"

    async def run(self, args: UnwatchInput, context: ToolContext) -> ToolResult:
        def stop() -> str | None:
            watch = context.db.get(WatchedFolder, args.watch_id)
            if watch is None or watch.user_id != context.user_id:
                return None
            if watch.status != "active":
                return "already stopped"
            watch.status = "stopped"
            context.db.commit()
            return watch.path

        outcome = await asyncio.to_thread(stop)

        if outcome is None:
            return ToolResult(ok=False, error=f"There is no watch #{args.watch_id}.")
        if outcome == "already stopped":
            return ToolResult(
                ok=False, error=f"Watch #{args.watch_id} was already stopped."
            )
        return ToolResult(
            output=(
                f"Stopped watching {outcome}. It can take up to a minute for "
                "the last events to stop arriving."
            )
        )


def build_watcher_tools() -> list[Tool]:
    return [WatchFolder(), ListWatchedFolders(), UnwatchFolder()]
