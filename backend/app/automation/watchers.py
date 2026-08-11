"""
Watching folders for changes.

NOTIFY-ONLY. A change becomes a notification and stops there. No filename
is ever built into a prompt, no model is called, no tool runs. See the
WatchedFolder docstring in db/models.py for why that is the design and not
merely the cautious option.

THE HARD PART IS THREADS, NOT FILES.

watchdog does not poll. It subscribes to the operating system's own change
notifications -- ReadDirectoryChangesW on Windows -- which is why a watched
folder costs nothing while nothing happens in it. But those notifications
arrive on watchdog's OWN BACKGROUND THREAD, and everything else in this
application runs on the asyncio event loop.

asyncio objects are not thread-safe. Calling hub.broadcast() from the
watchdog thread, or even touching an asyncio.Queue from it, is undefined
behaviour: it may work, it may deadlock, it may corrupt the loop's internal
state in a way that shows up somewhere else entirely an hour later. This is
the single most common way to introduce a bug that cannot be reproduced.

There is exactly one supported bridge, and this module uses it:

    loop.call_soon_threadsafe(callback, *args)

That is the ONLY asyncio method safe to call from another thread (with
run_coroutine_threadsafe, its sibling for coroutines). It hands the callback
to the loop, which runs it later on its own thread. So:

    watchdog thread          |  event loop
    -------------------------|---------------------------
    OS says "file changed"   |
    filter out the noise     |
    call_soon_threadsafe --------> queue.put_nowait(event)
                             |  _drain() wakes, debounces,
                             |  hub.broadcast(...)

The filtering happens on the watchdog thread deliberately: a build
directory can produce thousands of events a second, and discarding them at
the source is far cheaper than waking the loop for each one.

WHY EVENTS ARE DROPPED WHEN NOBODY IS CONNECTED.

A reminder waits, because you asked for it and it is a promise. A file
event is neither: it is unbounded in volume and stale within minutes.
Keeping a day of them would mean a queue with no ceiling and a "you have
40,000 notifications" on connect. So a change with nobody listening is
logged and dropped.
"""

import asyncio
import logging
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.automation.notifications import NotificationHub
from app.config import settings
from app.tools.paths import DENIED_NAMES

logger = logging.getLogger(__name__)

# Folders whose churn is noise rather than news. A git checkout rewrites
# every file in .git; a test run rewrites __pycache__; node_modules changes
# by the thousand on a single install.
IGNORED_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "chroma_data",
        "$RECYCLE.BIN",
        "System Volume Information",
    }
)

# Half-written files. Editors and browsers write to a temporary name and
# rename on completion, so reporting these announces files that never exist.
IGNORED_SUFFIXES = (
    ".tmp",
    ".temp",
    ".swp",
    ".part",
    ".crdownload",
    ".partial",
    "~",
)

IGNORED_PREFIXES = ("~$", ".~lock.")


def is_noise(path_text: str) -> bool:
    """
    Should this path be discarded before anyone is told about it?

    Runs on the watchdog thread, so it is deliberately plain string and
    pathlib work with no I/O -- no stat(), no exists(). The file may
    already be gone by the time we look, and a filesystem call per event
    would make a busy folder expensive.
    """
    path = Path(path_text)

    if any(part in IGNORED_DIRS for part in path.parts):
        return True

    # The same deny-list the filesystem tools use. A watcher only reports a
    # name, but "~/.ssh/id_rsa changed" is still information about a secret
    # and there is no reason to emit it.
    if any(part.lower() in DENIED_NAMES for part in path.parts):
        return True

    name = path.name
    if name.startswith(IGNORED_PREFIXES):
        return True
    return name.lower().endswith(IGNORED_SUFFIXES)


class _Handler(FileSystemEventHandler):
    """
    Receives raw events from watchdog, on watchdog's thread.

    Subclassing FileSystemEventHandler and overriding on_any_event is
    watchdog's designed extension point. The only thing this class is
    allowed to do with the event loop is call_soon_threadsafe.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        self._loop = loop
        self._queue = queue

    def on_any_event(self, event: FileSystemEvent) -> None:
        # Directory events are skipped: creating one file in a new folder
        # produces both, and the file is the interesting half.
        if event.is_directory:
            return
        if event.event_type not in {"created", "modified", "deleted", "moved"}:
            return

        path_text = str(event.dest_path or event.src_path)
        if is_noise(path_text):
            return

        payload = {
            "type": "file_event",
            "change": event.event_type,
            "path": path_text,
            "name": Path(path_text).name,
        }

        try:
            # THE bridge between threads. See the module docstring.
            self._loop.call_soon_threadsafe(self._queue.put_nowait, payload)
        except RuntimeError:
            # The loop closed while an event was in flight -- shutdown.
            # Expected, and not worth a traceback.
            pass


class WatcherService:
    """Watches folders and pushes changes to whoever is connected."""

    def __init__(self, hub: NotificationHub, session_factory) -> None:
        self._hub = hub
        self._session_factory = session_factory

        self._observer: Observer | None = None
        self._queue: asyncio.Queue | None = None
        self._drain_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        # path -> the watchdog handle, so a watch can be removed again.
        self._watches: dict[str, object] = {}

        # path -> when it was last reported, for debouncing.
        self._last_sent: dict[str, float] = {}

        # Rolling one-minute window, for the flood cap.
        self._window_started = 0.0
        self._window_count = 0
        self._suppressed = 0

    async def start(self) -> None:
        """Begin watching. Called once, from the app's lifespan hook."""
        if not settings.watchers_enabled:
            logger.info("File watchers are disabled")
            return

        # Captured here rather than in __init__ because there is no running
        # loop at import time -- only once the server is actually up.
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._observer = Observer()
        self._observer.start()
        self._drain_task = asyncio.create_task(self._drain())

        await self.sync()
        logger.info("File watchers started")

    async def stop(self) -> None:
        """Stop watching, releasing the OS handles."""
        if self._drain_task is not None:
            self._drain_task.cancel()
            self._drain_task = None

        if self._observer is not None:
            self._observer.stop()
            # join() blocks until the thread has finished, so it runs off
            # the loop -- otherwise shutdown would stall the whole server.
            await asyncio.to_thread(self._observer.join, 5)
            self._observer = None

        self._watches.clear()
        logger.info("File watchers stopped")

    async def sync(self) -> int:
        """
        Make the running watches match the database. Returns how many are active.

        Called on startup and on a timer, rather than the tools reaching in
        to add and remove watches directly. Same reasoning as the scheduler:
        the database is the only state, so a restart needs no recovery code
        and a watch added while the watcher was off simply starts working.
        """
        if self._observer is None:
            return 0

        wanted = await asyncio.to_thread(self._load_active_paths)

        for path in list(self._watches):
            if path not in wanted:
                self._unschedule(path)

        for path, recursive in wanted.items():
            if path not in self._watches:
                self._schedule(path, recursive)

        return len(self._watches)

    # --- internals ---------------------------------------------------------

    def _load_active_paths(self) -> dict[str, bool]:
        from sqlalchemy import select

        from app.db.models import WatchedFolder

        db = self._session_factory()
        try:
            rows = db.scalars(
                select(WatchedFolder).where(WatchedFolder.status == "active")
            ).all()
            return {r.path: r.recursive for r in rows}
        finally:
            db.close()

    def _schedule(self, path: str, recursive: bool) -> None:
        handler = _Handler(self._loop, self._queue)
        try:
            self._watches[path] = self._observer.schedule(
                handler, path, recursive=recursive
            )
            logger.info("Watching %s (recursive=%s)", path, recursive)
        except OSError as exc:
            # The folder was deleted or unmounted since it was added. Not
            # fatal -- the row stays and the watch starts if it comes back.
            logger.warning("Could not watch %s: %s", path, exc)

    def _unschedule(self, path: str) -> None:
        handle = self._watches.pop(path, None)
        if handle is not None and self._observer is not None:
            try:
                self._observer.unschedule(handle)
            except Exception:
                logger.info("Watch on %s was already gone", path)
        logger.info("Stopped watching %s", path)

    async def _drain(self) -> None:
        """Take events off the queue, debounce them, and push them out."""
        assert self._queue is not None
        while True:
            try:
                payload = await self._queue.get()
                await self._deliver(payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad event must not kill the drain task, or watching
                # silently stops working for the rest of the session.
                logger.error("Failed to deliver a file event", exc_info=True)

    async def _deliver(self, payload: dict) -> None:
        now = time.monotonic()
        path = payload["path"]

        # Debounce: one save produces several events for the same file.
        last = self._last_sent.get(path)
        if last is not None and now - last < settings.watch_debounce_seconds:
            return
        self._last_sent[path] = now

        # Bound the memory. Without this, watching a busy folder for a week
        # grows a dict entry per distinct path forever.
        if len(self._last_sent) > 2000:
            cutoff = now - settings.watch_debounce_seconds
            self._last_sent = {
                p: t for p, t in self._last_sent.items() if t > cutoff
            }

        if not self._within_rate_limit(now):
            return

        if self._hub.client_count == 0:
            # Nobody listening. Dropped rather than queued -- see the module
            # docstring on why a file event is not a promise.
            logger.debug("File event with nobody connected: %s", payload["name"])
            return

        await self._hub.broadcast(payload)

    def _within_rate_limit(self, now: float) -> bool:
        """
        Allow at most watch_max_events_per_minute, then go quiet.

        An unzip or a git checkout can produce thousands of events in a
        second. Sending them all would flood the socket and tell the user
        nothing they could read anyway, so past the cap one summary goes out
        and the rest are counted.
        """
        if now - self._window_started >= 60:
            if self._suppressed:
                logger.info("Suppressed %s file events in the last minute", self._suppressed)
            self._window_started = now
            self._window_count = 0
            self._suppressed = 0

        self._window_count += 1
        cap = settings.watch_max_events_per_minute

        if self._window_count <= cap:
            return True

        self._suppressed += 1
        if self._window_count == cap + 1:
            # Say so exactly once per window, so the user knows the silence
            # is deliberate rather than the watcher having died.
            asyncio.create_task(
                self._hub.broadcast(
                    {
                        "type": "file_event",
                        "change": "flood",
                        "path": "",
                        "name": (
                            f"Lots of files are changing at once -- pausing "
                            f"notifications for the rest of this minute."
                        ),
                    }
                )
            )
        return False
