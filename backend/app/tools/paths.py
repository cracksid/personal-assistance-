"""
The path sandbox.

Every filesystem tool resolves its paths through here. This is the second of
the three safety layers -- the gate decides *whether* something dangerous may
run, this decides *where* it may run at all.

Two independent checks, because either alone is insufficient:

  1. Containment -- the resolved path must sit inside FS_ROOT.
  2. Deny-list  -- some paths inside FS_ROOT are still off limits, because
                   a home directory holds ~/.ssh, ~/.aws, and JARVIS's own
                   .env. "Inside the sandbox" is not the same as "safe".

RESOLUTION MUST HAPPEN BEFORE THE CHECK. That ordering is the whole
security property. `~/../../Windows/System32` and a symlink pointing at
C:\\ both look innocent as strings and both escape a naive prefix test.
Path.resolve() collapses `..` and follows symlinks, so the check runs
against where the path *actually leads* rather than how it was spelled.

pathlib throughout, never string concatenation (CLAUDE.md, and this is
Windows -- backslashes, drive letters, and case-insensitivity all bite).
"""

import logging
from pathlib import Path

from app.config import settings
from app.tools.base import ToolError

logger = logging.getLogger(__name__)

# Names that are off limits even inside the sandbox. Compared
# case-insensitively against every component of the resolved path, so
# ~/.SSH/id_rsa and ~/projects/.env are both caught.
#
# This is a deny-list, which is inherently incomplete -- it stops the
# obvious catastrophes, not a determined attacker. The containment check
# above it is the real boundary.
DENIED_NAMES = frozenset(
    {
        ".env",
        ".ssh",
        ".aws",
        ".gnupg",
        ".netrc",
        ".git-credentials",
        ".password-store",
        "id_rsa",
        "id_ed25519",
        "credentials",
        "secrets",
    }
)


def sandbox_root() -> Path:
    """The one directory tree tools are allowed to touch."""
    return Path(settings.fs_root).expanduser().resolve()


def safe_resolve(raw_path: str) -> Path:
    """
    Turn a caller-supplied path into an absolute one we are willing to touch.

    Args:
        raw_path: whatever the model or the user asked for. Untrusted.

    Returns:
        The fully resolved path, guaranteed inside the sandbox and not
        deny-listed.

    Raises:
        ToolError: if the path escapes the sandbox or is deny-listed. The
            message deliberately says where the boundary is, because the
            usual cause is an honest mistake rather than an attack.
    """
    root = sandbox_root()

    if not raw_path or not raw_path.strip():
        raise ToolError("No path was given.")

    if "\x00" in raw_path:
        # Python raises on this at the filesystem call anyway, but the
        # message is an unhelpful ValueError from deep inside pathlib.
        raise ToolError("That path contains a null byte, which is not a real path.")

    text = raw_path.strip()

    # Refuse alternate data streams outright, rather than only normalising
    # them for the deny-list. An ADS is a second, invisible file hidden
    # inside a visible one -- "notes.txt:secret" does not appear in a
    # directory listing, in Explorer, or in anything JARVIS shows the user.
    # There is no legitimate reason for an assistant to read or write one,
    # and "content the user cannot see" is a bad thing to hand a model that
    # also reads pages written by strangers.
    #
    # The drive letter is the one legitimate colon, and it always sits at
    # position 1 ("C:\\..."), so anything further along is a stream.
    if ":" in text[2:]:
        raise ToolError(
            f"Refusing to touch {raw_path!r}: ':' names an alternate data "
            "stream, which is hidden from every normal view of the disk."
        )

    candidate = Path(text).expanduser()

    # A relative path is interpreted against the sandbox root, not the
    # process's working directory -- which the caller cannot see and which
    # changes depending on how the server was started.
    if not candidate.is_absolute():
        candidate = root / candidate

    # THE critical line. strict=False so a path that does not exist yet
    # still resolves -- write_file needs to validate its destination
    # before creating it.
    resolved = candidate.resolve(strict=False)

    if not _is_inside(resolved, root):
        raise ToolError(
            f"Path is outside the allowed area. JARVIS may only touch files "
            f"under {root}, and {resolved} is not."
        )

    denied = _denied_component(resolved)
    if denied is not None:
        raise ToolError(
            f"Refusing to touch {resolved}: {denied!r} holds credentials or "
            "configuration that tools must not read or modify."
        )

    return resolved


def _normalise_component(part: str) -> str:
    """
    Reduce a path component to the name Windows will actually open.

    THIS FUNCTION EXISTS BECAUSE OF A REAL BYPASS, found by trying it.

    The deny-list compares names, and Windows accepts several spellings of
    one file. Reading `.env.` -- with a trailing dot -- opened the real
    `.env`, because the filesystem ignores trailing dots and spaces, while
    the deny-list saw a string that was not on it. The API key was one
    request away. Measured, not theorised:

        '_sec_probe.txt'   -> 'REAL CONTENTS'
        '_sec_probe.txt.'  -> 'REAL CONTENTS'
        '_sec_probe.txt '  -> 'REAL CONTENTS'

    Alternate data streams are the same trick in another spelling:
    `.env::$DATA` is the file's own contents, and `.env:x` is a hidden
    stream attached to it.

    Normalising before comparing is the fix. Comparing raw strings against
    a list of names cannot work when the operating system does not compare
    names that way.
    """
    # "name:stream" and "name::$DATA" are streams of "name". Splitting takes
    # the file itself, which is what the deny-list should judge.
    part = part.split(":", 1)[0]
    # Windows silently drops trailing dots and spaces.
    return part.rstrip(". ").lower()


def _is_inside(path: Path, root: Path) -> bool:
    """
    Is `path` within `root`?

    Path.is_relative_to compares the parsed components rather than the raw
    strings, so it is not fooled by "C:/Users/Admin" versus
    "C:\\Users\\Admin\\", and on Windows it is case-insensitive -- which
    matters, because C:\\USERS\\ADMIN and C:\\Users\\Admin are the same
    directory and a plain string prefix test would say otherwise.
    """
    return path == root or path.is_relative_to(root)


def _denied_component(path: Path) -> str | None:
    """
    Return the first deny-listed component of `path`, if any.

    Each component is normalised first -- see _normalise_component. Without
    that, `.env.` and `.env:x` both walked straight past this check while
    Windows opened the real `.env`.
    """
    for part in path.parts:
        if _normalise_component(part) in DENIED_NAMES:
            return part
    return None


def describe_size(path: Path) -> str:
    """Human-readable file size, for confirmation prompts."""
    try:
        size = path.stat().st_size
    except OSError:
        return "unknown size"

    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
