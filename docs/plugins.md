# Writing a JARVIS plugin

A plugin adds a tool the model can call. It is a single `.py` file dropped
into the `plugins/` folder — no registration, no configuration, no changes to
JARVIS itself.

A plugin is not a second-class tool, and not a privileged one either. It goes
through the same confirmation gate, gets the same audit row, and declares
itself the same way a built-in does.

---

## The shortest possible plugin

Save this as `plugins/hello.py` and restart JARVIS:

```python
from app.plugins.sdk import BaseModel, Field, Tool, ToolContext, ToolResult


class GreetInput(BaseModel):
    name: str = Field(description="Who to greet.")


class Greet(Tool):
    name = "greet"
    description = "Say hello to someone by name."
    input_schema = GreetInput
    requires_confirmation = False

    def describe_action(self, args: GreetInput) -> str:
        return f"Greet {args.name}"

    async def run(self, args: GreetInput, context: ToolContext) -> ToolResult:
        return ToolResult(output=f"Hello, {args.name}!")


def register() -> list[Tool]:
    return [Greet()]
```

That's the whole contract. Check it loaded:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/plugins"
```

### Restart, and a trap worth knowing

Plugins are loaded once, at startup. Saving a file does nothing until JARVIS
restarts.

`--reload` will not save you here: uvicorn watches the directory it was
launched from, which is `backend/`, and `plugins/` is outside it. Saving a
plugin looks like it should reload and doesn't, which is a confusing five
minutes. Tell uvicorn to watch it too:

```powershell
python -m uvicorn app.main:app --reload --reload-dir . --reload-dir ..\plugins
```

Loading at startup rather than on demand is deliberate: the set of tools the
model is offered should not change midway through a conversation, and a
plugin that appears halfway through a turn is a debugging problem nobody
needs.

There's a longer, genuinely useful example in
[`plugins/example_units.py`](../plugins/example_units.py) — copy that rather
than this one if you're building something real.

---

## The five things a tool must declare

| | What it's for |
|---|---|
| `name` | What the model calls. Lowercase, underscores, unique. |
| `description` | **How the model decides to use it.** Say *when* to use it, not just what it does. |
| `input_schema` | A Pydantic model. Validated before your code runs. |
| `requires_confirmation` | Whether a human must approve first. |
| `describe_action()` | The sentence the user reads before approving. |

Plus `run()`, which does the work.

### `description` is a prompt, not a comment

It's the only thing the model sees when deciding whether to call your tool.
Compare:

```python
description = "Converts units."                       # vague — rarely called
description = ("Convert a measurement between units of length (mm, cm, m, "
               "km, in, ft, mi) or mass (g, kg, lb, oz). Use this instead "
               "of calculating the conversion yourself.")
```

The second lists what it handles *and* tells the model to prefer it over
doing the sum itself. Field descriptions matter for the same reason — they
become JSON Schema and are how the model learns what to pass.

### `describe_action()` is what someone approves

Only shown when `requires_confirmation = True`, and it must be concrete:

```python
return "Delete C:\\Users\\Admin\\notes.txt (2.4 KB)"   # good
return "Delete a file"                                 # meaningless to approve
```

---

## What your plugin must not do

**Never prompt the user.** Set `requires_confirmation = True` and the core
stops and asks. A tool that asks for itself is a tool that can forget to.

**Never check whether you're allowed to run.** By the time `run()` is called,
arguments are validated, an audit row is committed, and any confirmation has
been given. Re-checking is dead code that will drift out of step.

**Never open your own database session.** Take it from `context`:

```python
async def run(self, args, context: ToolContext) -> ToolResult:
    context.db          # SQLAlchemy session
    context.user_id     # who is acting
    context.memory      # the vector store, or None
```

**Never block the event loop.** `run()` is `async`. If you do something slow
and synchronous, put it on a thread:

```python
import asyncio

result = await asyncio.to_thread(something_slow, args.path)
```

Reading a small file is fine inline. Anything that could take a second is not.

---

## Failing well

Return `ok=False` for something the model could fix by trying again:

```python
return ToolResult(ok=False, error=f"I don't know the unit {args.unit!r}.")
```

Raise `ToolError` for a refusal — outside the sandbox, missing permission.
Both are caught, logged and recorded; neither crashes anything. The message
goes to the model *and* the user, so write it for a human.

Unexpected exceptions are caught too, but the user sees only a generic
message. Prefer being explicit.

---

## Touching the filesystem

Use the same sandbox the built-in tools use, and let it do the checking:

```python
from app.tools.paths import safe_resolve

target = safe_resolve(args.path)   # raises ToolError if it escapes FS_ROOT
```

`safe_resolve` resolves `..` and symlinks *before* checking containment,
which a string comparison cannot do. Don't roll your own.

---

## Why it loads the way it does

`import x` searches `sys.path`, and a file you dropped in ten seconds ago
isn't on it. Putting `plugins/` on `sys.path` would be worse than useless: a
plugin named `logging.py` would shadow the standard library for the entire
process.

So the loader uses `importlib` pointed at an exact file, and names the module
`jarvis_plugin_<stem>`. A plugin called `json.py` becomes
`jarvis_plugin_json`, and the real `json` is untouched. There's a test that
asserts exactly that.

**Built-ins are registered first, and names cannot be reused.** A plugin
named `delete_file` is rejected — it cannot replace the built-in that asks
for confirmation with one that doesn't.

**One bad plugin is skipped, not fatal.** A syntax error, a missing
`register()`, a duplicate name, or an exception at import time is recorded
with its reason and JARVIS starts without it. `GET /plugins` shows you why.

**Files starting with `_` are ignored**, so `_helpers.py` is yours.

---

## The security position, stated plainly

**A plugin is ordinary Python running in JARVIS's process with JARVIS's
permissions.** It can open sockets, read files outside `FS_ROOT`, and import
anything installed. There is no sandbox and the SDK doesn't pretend there is.

What the gate does guarantee is narrower, and still worth having:

- nothing declared as a `Tool` runs without an audit row written first
- nothing declared destructive runs without a human "yes"
- nothing declared destructive runs *at all* during an unattended scheduled
  task
- no plugin can take a built-in's name

Those hold because the core enforces them, not because plugins are polite.
But a plugin that never declares a `Tool` and just runs code at import time
is outside all of it.

**Install plugins you trust, the way you would treat any Python package.**
There is deliberately no "install plugin" endpoint — adding one is a decision
made by putting a file in a folder, by the person at the keyboard.

To turn the whole mechanism off:

```
PLUGINS_ENABLED=false
```

---

## Checklist

- [ ] File in `plugins/`, name doesn't start with `_`
- [ ] `register()` returns a list of instances (not classes)
- [ ] Every tool name is unique and not a built-in's
- [ ] `description` says *when* to use it
- [ ] Every field has a `description`
- [ ] `requires_confirmation = True` if it changes or destroys anything
- [ ] `describe_action()` names the specific thing affected
- [ ] Slow work is on a thread
- [ ] Paths go through `safe_resolve`
- [ ] `GET /plugins` shows `loaded: true`
