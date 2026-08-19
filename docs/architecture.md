# Architecture

How JARVIS is put together, and why each piece is the shape it is.

Written for someone reading the codebase for the first time — including
future-you. Where a decision had a real alternative, the alternative is named
and the reason for rejecting it is given, because a decision without its
reasoning gets undone by the next person who has a good idea.

| | |
|---|---|
| `backend/app` | 8,750 lines across 66 files |
| `backend/tests` | 6,268 lines, 368 tests, 86% coverage |
| `frontend/src` | 2,944 lines |
| Runtime | Python 3.11, FastAPI, SQLite, Chroma; React + TypeScript; Electron |

---

## The shape

Six tiers. **Arrows only point downward — a lower tier never imports from a
higher one.**

```
Interfaces      React chat UI · Electron shell · voice controls
     |
API layer       FastAPI — WebSocket for chat, REST for everything else
     |
Core            THE AGENT LOOP. The only part that knows it is an assistant.
     |
     +-- Capabilities   tools + plugins (one interface, no second system)
     +-- Providers      LLM / STT / TTS / vision behind abstract base classes
     +-- Memory         short-term messages, long-term vectors, structured rows
```

That rule is what keeps the pieces replaceable. `memory/` has no idea an LLM
exists — it stores sentences and finds them again. `providers/` has no idea
what an assistant is — it moves text to and from a model. Only `core/` knows
it is running an assistant, and that is where every assistant-shaped decision
lives.

The clearest evidence it holds: the voice client in `voice/` drives the entire
system through the public API and imports nothing from `backend/app`. If the
layering were fictional, that would have been impossible.

---

## The agent loop

`core/agent.py`, and everything else exists to serve it.

```
save the user's message
  -> search long-term memory for relevant facts
  -> build the system prompt from them
  -> load recent messages (short-term memory)
  -> ask the model, offering the tool list
  -> stream the reply out as it arrives
  -> if it asked for tools: run them THROUGH THE GATE, feed results back, ask again
  -> if a tool needs confirmation: end the turn and ask the human
  -> save the finished reply
  -> afterwards: decide what was worth remembering, and store it
```

**Why a confirmation ends the turn rather than pausing it.** The obvious
implementation parks the coroutine and waits for an answer. That holds a
database session, an open provider connection and half-built history for as
long as someone takes to decide — possibly forever. Instead the loop yields the
request and returns; approval arrives as a separate call and the result is fed
back as a fresh turn. Every wait is explicit and every resource is released.

**Why fact extraction runs after the reply is sent.** It is a second model
call. Doing it before "done" would make the user wait for something they never
asked for.

---

## The gate

`core/gate.py` is the single choke point every tool execution passes through.
There is no other path — the API layer holds no reference to a tool's `run()`.

```
look up the tool           unknown name -> refuse, and log what was wanted
validate the arguments     against the tool's Pydantic schema
needs confirmation?        -> STOP. Return a description. Nothing has run.
write the audit row        AND COMMIT IT
run the tool
update the row             success or error
```

**A tool never asks "are you sure?" itself.** It declares
`requires_confirmation` and a `describe_action()`, and the core decides. The
reason is structural: if each tool implemented its own check, tool number
forty — written months later, possibly by a plugin author — would eventually
forget, and the failure would be silent and destructive. A tool that forgets
to *declare* is merely ungated; a tool that forgets to *ask* is unstoppable.

**The audit row is committed before execution, not after.** A crash during the
destructive part still leaves a row naming what was attempted. Writing it
afterwards would mean the worst case leaves no evidence.

**What gets approved is exactly what runs.** The confirmation carries an id;
the arguments stay server-side. Handing the arguments back to the client would
let one thing be approved and another executed.

**Unattended runs refuse instead of queueing.** A scheduled task at 8am has
nobody to approve anything. An approval prompt that outlives the moment invites
a "yes" hours later to a request the user was never present for.

---

## The tiers, one at a time

### Providers — `providers/`

Every model sits behind an abstract base class: `LLMProvider`, `STTProvider`,
`TTSProvider`, `VisionProvider`, `OCRProvider`. Nothing above them names a
vendor. Switching model is one line in `.env`, or a dropdown in the settings
panel.

The adapters do more than rename fields. Anthropic wants tool results as
`tool_result` blocks grouped inside a following **user** message; Ollama makes
them their own role and supplies **no call ids at all**, so the adapter mints
them. That reshaping is the entire justification for the layer.

`stream_turn()` was added for tool calling as a **concrete** method, not an
abstract one. Its default adapts `stream_chat` and ignores tools — so every
provider written before tools existed, including test fakes, kept working
untouched.

### Memory — `memory/`

Three kinds, deliberately separate:

| Kind | Where | Lifetime |
|---|---|---|
| Short-term | `messages` table | the conversation |
| Long-term | `facts` table + Chroma vectors | forever, until deleted |
| Structured | reminders, tasks, watches, audit | until acted on |

**SQLite is the source of truth; Chroma is derived.** The index can be deleted
and rebuilt from the table, and `main.py` heals an empty one on startup —
because the symptom of a lost index is silence, not an error.

**Deduplication is exact-text, not semantic.** The obvious idea — skip a fact
whose nearest neighbour is closer than some threshold — was tried and killed by
measurement: reworded duplicates sat 0.229–0.290 apart while "dark mode" and
"light mode" sat 0.131 apart. Any threshold catching the duplicates would have
merged the opposites. It was fixed upstream instead, by showing the extractor
what is already known.

### Capabilities — `tools/` and `plugins/`

One `Tool` interface: name, description, Pydantic input schema, `run()`,
`requires_confirmation`, `describe_action()`. Built-ins and plugins are the
same kind of thing, which is why the plugin phase needed a loader and nothing
else.

Two guards sit under the filesystem tools:

- **`paths.py`** — resolve *before* checking, so `..` and symlinks are followed
  first. A string comparison cannot do this.
- **`urls.py`** — resolve the hostname *before* checking the address, because
  plenty of public domains resolve to `127.0.0.1`.

Both had the same lesson land twice: a guard that inspects the input as
written, rather than as the system will interpret it, is not a guard.

### Automation — `automation/`

**One polling job, not one job per reminder.** APScheduler asks the database
every few seconds whether anything is due. Less precise, and worth it: the
scheduler holds no state, so sleeping the laptop or restarting the server
requires no recovery code at all.

The three subsystems differ in exactly one way — what happens when nobody is
connected — and each answer follows from what the thing *is*:

| | If nobody is listening |
|---|---|
| Reminder | **waits.** You asked for it; it is a promise. |
| Scheduled task | **does not run.** Generating it costs a model call. |
| File event | **is dropped.** Unbounded in volume, stale in minutes. |

Watchers are **notify-only**, and that is the design rather than caution. A file
appearing is input from outside; feeding a filename to the model would make
anyone who can write into a watched folder an author of instructions for an
agent holding tools, with nobody present. There is no rule to enforce because
there is no agent turn.

### API — `api/`

WebSocket for chat, because a reply arrives in many pieces over time. REST for
everything else, because they are one-shot. Dependencies are declared with
`Depends` rather than constructed inline, which is what lets the tests swap the
database, the model and the gate without touching route code.

### Interfaces — `frontend/`, `desktop/`

**The app contains no host and no port anywhere.** It opens `/ws/chat` and
fetches `/settings`. In development Vite proxies those to the backend; in
production FastAPI serves the built files, so the UI and API share an origin.
The same code works in both, with no build-time switch.

A dev proxy rather than CORS, deliberately: CORS is the backend announcing it
will talk to other origins, which is the opposite of "bind to 127.0.0.1".

Frames become UI entries **on arrival** rather than being stored raw — a reply
is dozens of chunk frames and one message. Translating once keeps every
component trivial.

---

## Decisions, and what they cost

| Decision | Instead of | Because |
|---|---|---|
| SQLite | Postgres | One user, one machine. SQLAlchemy makes the switch a connection string. |
| Chroma | FAISS | Chroma is a database — metadata filtering and persistence included. FAISS would mean building the document store. |
| APScheduler | Celery + Redis | No message broker for a single-user desktop app. |
| Electron | Tauri | Tauri needs a Rust toolchain and MSVC on Windows; Electron reuses the Node the frontend already requires. |
| Vite | CRA / Next.js | CRA is unmaintained; Next is a server framework, and the frontend must stay a plain web app so the shell is swappable. |
| Bind to 127.0.0.1 | Login system | The OS account is the boundary. A login protects nothing on a single-user desktop. |
| `faster-whisper` locally | Browser SpeechRecognition | The browser API sends audio to Google, which would quietly undo the reason for running Whisper at all. |
| `open_app` by name | `run_command` | A tool taking a command line accepts *every* command line, and approving a sentence cannot distinguish them. |

---

## What the bugs taught

The most useful section, and the least usual. **Every serious bug in this
project was found by using it, not by testing it** — and all of them lived in
code the suite already executed.

**`.env` was never loaded, for three phases.** `env_file=".env"` is relative
and resolved against the working directory. Every setting silently fell back to
its default, invisible because each default happened to equal the real value.
*Lesson: configuration that fails by being silently absent needs a test that it
was actually found.*

**Tests wrote to the real database.** The chat WebSocket depends on
`get_scheduler`, and the tests overrode everything else. A test connected, the
scheduler read the real `jarvis.db`, found a genuine pending reminder and
marked it delivered. Running the suite destroyed real data, and tests passed or
failed depending on the developer's own database. *Lesson: an autouse fixture,
so the next route that reaches for a real singleton cannot reintroduce it.*

**Chat resumed a scheduled task's conversation.** Tasks were given their own
threads so their turns stayed out of the user's chat — but "resume the newest
conversation" then picked up the task's. The user's next message landed in a
thread they had never seen, and the model answered from it. It looked exactly
like the small model being stupid. *Lesson: isolation in one direction is not
isolation.*

**A failed tool rendered as a blank line.** The event carried `output`, which
is empty on failure; the reason lived in `error`. The model was told what went
wrong and the user was shown nothing. *Lesson: the human and the model need the
same information.*

**A fact was stored as `The user’s favourite tea`.** The model
double-escaped its own JSON. It had been going into every prompt since, and
nothing surfaced it until the memory viewer existed. *Lesson: memory you cannot
inspect is memory you cannot trust.*

**One dropped WebSocket killed the desktop app.** Acceptable in a browser —
press F5. Phase 14 removed the F5. *Lesson: shipping a new shell changes what
counts as a bug.*

**SSRF through a redirect.** `safe_url` checked the URL it was given, then httpx
followed a `302` anywhere — and the result was still labelled as coming from the
public site. *Lesson: a guard that runs once, on the first URL, is not a guard.*

**`read_file(".env.")` returned the API key.** Windows ignores trailing dots, so
the deny-list compared a name the filesystem does not. *Lesson: compare inputs
the way the system will interpret them, not the way they were written.*

The pattern is consistent enough to be worth stating: **tests confirm what you
thought of; using it finds what you did not.** The coverage configuration says
so out loud, so the number is not mistaken for safety.

---

## Known limitations

- **No plugin sandbox.** A plugin is ordinary Python with the process's
  permissions. The gate covers what is declared as a `Tool`; import-time code is
  outside all of it.
- **Prompt injection is mitigated, not solved.** Labelling fetched content is a
  nudge. The guarantee is structural: nothing destructive runs without a human
  approving a specific description.
- **`FS_ROOT` defaults to the whole home directory.** A large blast radius,
  narrowed by one line in `.env`.
- **Local models are the weak link.** llama3.2 (3B) misfires on tool selection,
  fact extraction and instruction-following. Every "JARVIS is broken" moment in
  development traced to model quality, not code. Claude gets the same code
  right.
- **The speech and vision adapters are lightly tested** (32–45%). Testing them
  means faking onnxruntime and faster-whisper, and those tests would mostly
  assert that the mocks behave as written.

---

## Where to start reading

1. **`core/agent.py`** — the loop. Everything else is in service of it.
2. **`core/gate.py`** — the safety property the whole design rests on.
3. **`tools/base.py`** — the interface, and what it deliberately withholds.
4. **`providers/base.py`** — the contract that makes the model swappable.
5. **`tools/paths.py`** and **`tools/urls.py`** — the two guards, and the same
   lesson learned twice.

Then `docs/plugins.md` to extend it, and `docs/security-review.md` for what was
attacked and what held.
