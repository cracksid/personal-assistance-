"""
Loading plugins from a folder.

CLAUDE.md: "Plugins are loaded from a folder instead of imported. Same
interface, no second system."

IMPORTING A FILE THAT IS NOT ON THE IMPORT PATH.

`import x` searches sys.path -- a fixed list of directories decided when
Python started. A plugin dropped into plugins/ ten seconds ago is not on it,
and shouldn't be: adding a user-writable directory to sys.path would let a
file named `logging.py` shadow the standard library for the whole process.

importlib does the same job as the `import` statement, but pointed at an
exact file:

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)   # empty module object
    spec.loader.exec_module(module)                  # run the file into it

That last line is where the plugin's code actually executes. Everything a
plugin does at module level happens there, which is why it is wrapped.

THE CONTRACT.

A plugin is one .py file exposing register(), returning a list of Tools:

    from app.plugins.sdk import Tool, ToolResult, BaseModel

    class MyTool(Tool):
        ...

    def register() -> list[Tool]:
        return [MyTool()]

Explicit rather than clever. The obvious alternative is to scan the module
for Tool subclasses and instantiate them automatically, which is less
typing and much worse to debug: a class defined for a base, or imported
from elsewhere for reference, silently becomes a live tool. register() says
exactly what is offered.

ONE BAD PLUGIN MUST NOT TAKE THE SERVER DOWN.

Every stage is wrapped and reported, never raised. A plugin with a syntax
error, a missing register(), a duplicate tool name, or an exception at
import time is skipped with its reason recorded -- the assistant starts
without it. A crash on startup because of a third-party file in a folder
would be the worst possible failure mode for this feature.
"""

import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.tools import registry
from app.tools.base import Tool

logger = logging.getLogger(__name__)


@dataclass
class PluginReport:
    """
    What happened to one plugin file.

    Kept rather than only logged, because "why isn't my plugin loading?" is
    the first question every plugin author asks, and the answer should be
    available through the API instead of by reading a terminal.
    """

    name: str
    path: str
    loaded: bool
    tools: list[str] = field(default_factory=list)
    error: str | None = None


# The result of the last load_plugins() call. Module-level because there is
# one plugin folder per process, and the API layer needs to read it.
_reports: list[PluginReport] = []


def plugins_dir() -> Path:
    """The folder plugins are read from."""
    return Path(settings.plugins_dir).expanduser().resolve()


def reports() -> list[PluginReport]:
    """What happened on the last load. Empty before the first one."""
    return list(_reports)


def load_plugins() -> list[PluginReport]:
    """
    Load every plugin in the folder and register its tools.

    Returns one report per file found. Never raises: a plugin folder is
    user-controlled, and nothing in it should be able to stop JARVIS
    starting.
    """
    global _reports
    _reports = []

    if not settings.plugins_enabled:
        logger.info("Plugins are disabled")
        return _reports

    folder = plugins_dir()
    if not folder.is_dir():
        # Not an error. Most installs have no plugins, and creating the
        # folder on demand is friendlier than requiring it up front.
        logger.info("No plugin folder at %s", folder)
        return _reports

    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            # _helpers.py and __init__.py are the author's own scaffolding,
            # not plugins. Skipping them silently is the convention Python
            # programmers already expect.
            continue
        _reports.append(_load_one(path))

    loaded = sum(1 for r in _reports if r.loaded)
    if _reports:
        logger.info(
            "Loaded %s of %s plugin(s) from %s", loaded, len(_reports), folder
        )
    return _reports


def _load_one(path: Path) -> PluginReport:
    """Load a single plugin file, capturing any failure as a report."""
    name = path.stem
    report = PluginReport(name=name, path=str(path), loaded=False)

    try:
        module = _import_file(path)
    except Exception as exc:
        # Covers syntax errors, imports of packages that are not installed,
        # and anything the file does at module level.
        report.error = f"Could not import: {exc!r}"
        logger.error("Plugin %s failed to import", name, exc_info=True)
        return report

    entry = getattr(module, "register", None)
    if entry is None or not callable(entry):
        report.error = (
            "No register() function. A plugin must define "
            "def register() -> list[Tool]."
        )
        logger.warning("Plugin %s has no register()", name)
        return report

    try:
        produced = entry()
    except Exception as exc:
        report.error = f"register() raised: {exc!r}"
        logger.error("Plugin %s register() failed", name, exc_info=True)
        return report

    if not isinstance(produced, list) or not all(
        isinstance(t, Tool) for t in produced
    ):
        report.error = "register() must return a list of Tool instances."
        logger.warning("Plugin %s returned something other than tools", name)
        return report

    # Register one at a time so a single bad tool does not lose the rest,
    # and so a name collision names the plugin that caused it.
    for tool in produced:
        problem = _validate(tool)
        if problem is not None:
            report.error = problem
            logger.warning("Plugin %s: %s", name, problem)
            continue
        try:
            registry.register(tool)
        except ValueError as exc:
            # registry.register refuses to shadow an existing name. That is
            # the rule that stops a plugin called "delete_file" replacing
            # the built-in that asks for confirmation with one that does not.
            report.error = str(exc)
            logger.warning("Plugin %s: %s", name, exc)
            continue
        report.tools.append(tool.name)

    report.loaded = bool(report.tools)
    if report.loaded:
        logger.info("Plugin %s provided: %s", name, ", ".join(report.tools))
    return report


def _import_file(path: Path):
    """
    Execute a .py file as a module, without putting its folder on sys.path.

    The module name is prefixed so a plugin called `json.py` becomes
    `jarvis_plugin_json` in sys.modules and cannot be mistaken for, or
    shadow, the real thing.
    """
    module_name = f"jarvis_plugin_{path.stem}"

    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{path} is not importable")

    module = importlib.util.module_from_spec(spec)

    # Registered before exec_module so a plugin that imports itself, or uses
    # dataclasses/pickle (both of which look modules up by name), works.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        # Do not leave a half-executed module behind for the next attempt
        # to find.
        sys.modules.pop(module_name, None)
        raise
    return module


def _validate(tool: Tool) -> str | None:
    """
    Check a plugin's tool declares what the gate and the model need.

    A built-in with a missing description fails loudly in review. A plugin
    is written by someone else, so it is checked instead -- and rejected
    with a message that says what to fix.
    """
    for attribute in ("name", "description", "input_schema"):
        if not getattr(tool, attribute, None):
            return f"{type(tool).__name__} is missing {attribute!r}."

    if not isinstance(tool.name, str) or not tool.name.replace("_", "").isalnum():
        return (
            f"{tool.name!r} is not a usable tool name. Use lowercase letters, "
            "digits and underscores."
        )

    if not isinstance(tool.input_schema, type):
        return f"{tool.name}: input_schema must be a Pydantic model class."

    return None
