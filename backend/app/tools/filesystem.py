"""
File system tools.

Five capabilities, split by how much damage they can do:

    read-only   list_directory, read_file, search_files
    destructive write_file, delete_file      <- requires_confirmation = True

Notice what none of them do: none prompts, none checks whether it is
allowed, none writes to the audit log. Every one of them calls
safe_resolve() and then does its job. The gate handles the rest.

Every path goes through safe_resolve() -- that is not optional, and it is
the single line that keeps a tool inside the sandbox.
"""

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from app.config import settings
from app.tools.base import Tool, ToolError, ToolResult
from app.tools.paths import describe_size, safe_resolve, sandbox_root


# --- inputs ----------------------------------------------------------------
# One Pydantic model per tool. These are what the model's JSON is validated
# against, so a missing or mistyped field fails before any tool code runs.


class PathInput(BaseModel):
    path: str = Field(description="Path to the file or folder.")


class WriteInput(BaseModel):
    path: str = Field(description="Path of the file to write.")
    content: str = Field(description="Text to write into the file.")


class SearchInput(BaseModel):
    pattern: str = Field(
        description="Glob pattern, e.g. '*.py' or '**/notes*.md'."
    )
    path: str = Field(default=".", description="Folder to search in.")


# --- read-only tools -------------------------------------------------------


class ListDirectory(Tool):
    name = "list_directory"
    description = (
        "List the files and folders in a directory. Use this to find out what "
        "exists before reading or writing anything."
    )
    input_schema = PathInput
    requires_confirmation = False

    def describe_action(self, args: PathInput) -> str:
        return f"List the contents of {args.path}"

    async def run(self, args: PathInput) -> ToolResult:
        target = safe_resolve(args.path)
        return await asyncio.to_thread(self._list, target)

    def _list(self, target: Path) -> ToolResult:
        if not target.exists():
            return ToolResult(ok=False, error=f"{target} does not exist.")
        if not target.is_dir():
            return ToolResult(ok=False, error=f"{target} is a file, not a folder.")

        entries = sorted(
            target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
        )
        shown = entries[: settings.fs_max_entries]

        lines = [
            f"{'[dir] ' if e.is_dir() else '      '}{e.name}" for e in shown
        ]
        if len(entries) > len(shown):
            lines.append(f"... and {len(entries) - len(shown)} more")

        body = "\n".join(lines) if lines else "(empty)"
        return ToolResult(output=f"{target}\n{body}")


class ReadFile(Tool):
    name = "read_file"
    description = (
        "Read the contents of a text file. Returns the beginning of the file "
        "if it is very large."
    )
    input_schema = PathInput
    requires_confirmation = False

    def describe_action(self, args: PathInput) -> str:
        return f"Read {args.path}"

    async def run(self, args: PathInput) -> ToolResult:
        target = safe_resolve(args.path)
        return await asyncio.to_thread(self._read, target)

    def _read(self, target: Path) -> ToolResult:
        if not target.exists():
            return ToolResult(ok=False, error=f"{target} does not exist.")
        if target.is_dir():
            return ToolResult(
                ok=False, error=f"{target} is a folder. Use list_directory."
            )

        limit = settings.fs_max_read_bytes
        try:
            # errors="replace" rather than raising: a stray non-UTF-8 byte in
            # a log file should not make the file unreadable.
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(ok=False, error=f"Could not read {target}: {exc}")

        if len(text) > limit:
            text = text[:limit] + f"\n\n[truncated at {limit} characters]"
        return ToolResult(output=text)


class SearchFiles(Tool):
    name = "search_files"
    description = (
        "Find files by name using a glob pattern. Use '**/' at the start of "
        "the pattern to search inside subfolders too."
    )
    input_schema = SearchInput
    requires_confirmation = False

    def describe_action(self, args: SearchInput) -> str:
        return f"Search for {args.pattern!r} in {args.path}"

    async def run(self, args: SearchInput) -> ToolResult:
        root = safe_resolve(args.path)
        return await asyncio.to_thread(self._search, root, args.pattern)

    def _search(self, root: Path, pattern: str) -> ToolResult:
        if not root.is_dir():
            return ToolResult(ok=False, error=f"{root} is not a folder.")

        matches: list[str] = []
        try:
            for found in root.glob(pattern):
                # Re-check every hit. A glob can follow a symlink out of the
                # sandbox, so validating only the starting folder is not
                # enough -- results have to be filtered too.
                try:
                    safe_resolve(str(found))
                except ToolError:
                    continue
                matches.append(str(found))
                if len(matches) >= settings.fs_max_entries:
                    matches.append("... (more results not shown)")
                    break
        except (OSError, ValueError) as exc:
            return ToolResult(ok=False, error=f"Search failed: {exc}")

        if not matches:
            return ToolResult(output=f"Nothing matched {pattern!r} in {root}.")
        return ToolResult(output="\n".join(matches))


# --- destructive tools -----------------------------------------------------


class WriteFile(Tool):
    name = "write_file"
    description = (
        "Write text to a file, creating it if needed and REPLACING it "
        "entirely if it already exists."
    )
    input_schema = WriteInput
    # Overwriting is silent and irreversible, so this one asks first.
    requires_confirmation = True

    def describe_action(self, args: WriteInput) -> str:
        # Concrete, not vague -- the user is approving on the strength of
        # this sentence, so it has to say whether data will be destroyed.
        try:
            target = safe_resolve(args.path)
        except ToolError:
            target = Path(args.path)

        size = len(args.content)
        if target.exists():
            return (
                f"OVERWRITE the existing file {target} "
                f"({describe_size(target)}) with {size} characters of new text. "
                "The current contents will be lost."
            )
        return f"Create a new file {target} containing {size} characters."

    async def run(self, args: WriteInput) -> ToolResult:
        target = safe_resolve(args.path)
        return await asyncio.to_thread(self._write, target, args.content)

    def _write(self, target: Path, content: str) -> ToolResult:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(ok=False, error=f"Could not write {target}: {exc}")

        return ToolResult(output=f"Wrote {len(content)} characters to {target}.")


class DeleteFile(Tool):
    name = "delete_file"
    description = "Delete a single file. Does not delete folders."
    input_schema = PathInput
    requires_confirmation = True

    def describe_action(self, args: PathInput) -> str:
        try:
            target = safe_resolve(args.path)
        except ToolError:
            return f"Delete {args.path}"

        if target.exists():
            return f"DELETE the file {target} ({describe_size(target)}). This cannot be undone."
        return f"Delete {target} (which does not currently exist)."

    async def run(self, args: PathInput) -> ToolResult:
        target = safe_resolve(args.path)
        return await asyncio.to_thread(self._delete, target)

    def _delete(self, target: Path) -> ToolResult:
        if not target.exists():
            return ToolResult(ok=False, error=f"{target} does not exist.")
        if target.is_dir():
            # Recursive directory deletion is the single most destructive
            # thing a tool could do, so it is simply not offered. A user who
            # genuinely wants it can do it themselves.
            return ToolResult(
                ok=False,
                error=f"{target} is a folder. This tool only deletes single files.",
            )

        try:
            target.unlink()
        except OSError as exc:
            return ToolResult(ok=False, error=f"Could not delete {target}: {exc}")

        return ToolResult(output=f"Deleted {target}.")


def build_filesystem_tools() -> list[Tool]:
    """Every filesystem tool, ready to register."""
    return [
        ListDirectory(),
        ReadFile(),
        SearchFiles(),
        WriteFile(),
        DeleteFile(),
    ]


__all__ = [
    "ListDirectory",
    "ReadFile",
    "SearchFiles",
    "WriteFile",
    "DeleteFile",
    "build_filesystem_tools",
    "sandbox_root",
]
