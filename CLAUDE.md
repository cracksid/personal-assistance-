# JARVIS — personal AI assistant

## What this is

A modular personal AI assistant: voice conversation, persistent memory, web search,
document and image analysis, coding help, file and system control, calendar, email,
automation, and a plugin system.

Single user. Runs locally on Windows. Not a hosted multi-tenant service.

## Who you're working with

Sid. Python level: beginner — comfortable with variables, loops, and functions.
No backend, async, or web framework experience yet. Teaching mode is mandatory.

## Working rules — read these first

- Build **one phase at a time**. Stop at the end of each phase and wait for Sid to say go.
  Do not scaffold ahead into future phases "while you're in there."
- Before writing code for a module: explain what it is, why it's needed, and how it works.
  Then write the code. Then explain how to run it and how to test it.
- Explain every Python concept the first time it appears — virtual environments, type hints,
  decorators, async/await, abstract base classes, dependency injection, context managers,
  Pydantic models, ORMs. Assume no prior exposure.
- Prefer boring, readable code over clever code. This codebase is also a teaching artifact.
- When a decision has a real tradeoff, state both sides and recommend one. Don't silently pick.
- Ask before adding a dependency that isn't already in the current phase's plan.
- Keep responses focused. Don't dump 15 files at once — a phase is a handful of files.

## Architecture — decided, don't relitigate

Six tiers. **Arrows only point downward: a lower tier must never import from a higher one.**

```
Interfaces      React chat UI, voice controls, Electron shell
     |
API layer       FastAPI — WebSocket for chat streaming, REST for everything else
     |
Core            THE AGENT LOOP. The only part that knows it's an assistant.
     |
     +-- Capabilities   tools + plugins (same interface)
     +-- Providers      LLM / STT / TTS / vision adapters behind ABCs
     +-- Memory         short-term, long-term vectors, structured tables
```

The loop: input -> assemble context (recent messages + retrieved memories + tool list)
-> model decides answer or tool call -> confirmation gate if sensitive -> execute
-> feed result back -> stream response out -> write anything worth remembering to memory.

## Locked decisions and the reasoning

- **Chroma, not FAISS** — Chroma is an embedded database with metadata filtering and
  persistence built in. FAISS is a bare similarity-search library that would mean
  building the document store ourselves.
- **SQLite throughout, not Postgres** — this is a single-user desktop app. Use SQLAlchemy
  so switching to Postgres is one connection string if it's ever needed.
- **Electron, not Tauri** — Tauri needs a Rust toolchain and MSVC build tools on Windows.
  Electron reuses the Node install the frontend already requires. The frontend stays a
  plain web app so the shell is swappable.
- **APScheduler + FastAPI BackgroundTasks, not Celery/Redis** — no message broker for a
  single-user app. Revisit only if scheduled jobs genuinely outgrow it.
- **Auth = bind to 127.0.0.1 + gitignored .env + encryption at rest + audit log.**
  Not OAuth, not JWT refresh flows. Build the user table and role column so multi-user is
  possible later, but don't build a login system that isn't needed.

## Non-negotiable design rules

- **The confirmation gate is exactly one choke point in `core/`.** Individual tools must never
  implement their own "are you sure?" prompt. A tool declares `requires_confirmation: bool`
  and a `describe_action()` method; the core decides. This is so a forgotten check in tool
  number 40 can't delete a folder.
- Every tool — built-in or plugin — implements the same `Tool` interface: name, description,
  Pydantic input schema, `run()`, `requires_confirmation`, `describe_action()`.
- Plugins are loaded from a folder instead of imported. Same interface, no second system.
- Providers sit behind abstract base classes (`LLMProvider`, `STTProvider`, `TTSProvider`).
  Never hardcode a provider name anywhere in `core/`. Switching models is a config change.
- An audit log row is written **before** any tool executes, not after.
- All filesystem paths go through `pathlib`. Never string concatenation. This is Windows.
- Secrets live in `.env`, which is gitignored. Never commit a key. Never log a key.

## Conventions

- Python 3.11+, type hints on every function signature
- Pydantic v2 for all schemas and settings
- SQLAlchemy 2.0 style
- pytest, tests written in the same phase as the code they cover
- Structured logging via the `logging` module — no `print()` in application code
- `ruff` for lint and format

## Windows notes

- `sounddevice` for audio, not `PyAudio` (broken wheels on Windows)
- `faster-whisper` for speech-to-text — runs on CPU, uses NVIDIA GPU if present
- `subprocess` + Start Menu shortcut resolution for launching apps
- Docker is Phase 16 and optional; it needs WSL2 on Windows

## Folder structure

```
backend/app/    main.py, api/, core/, providers/, memory/, tools/,
                plugins/, automation/, db/, security/, config/
backend/tests/
frontend/       React + TypeScript
desktop/        Electron wrapper (Phase 14)
docs/           architecture, module docs, plugin guide
deployment/     Dockerfile, compose, GitHub Actions
```

## Phase plan

1. Architecture — **DONE**
2. Project setup — venv, skeleton, git, config/.env loading, FastAPI health check
3. Backend — routing, WebSocket streaming, error handling, logging
4. Database — SQLAlchemy models, sessions, migrations
5. AI integration — provider ABCs, first LLM adapter, the agent loop
6. Memory — short-term, Chroma vectors, fact extraction, structured tables
7. Voice — STT, TTS, wake word, push-to-talk
8. Vision — screenshots, image analysis, OCR
9. File system tools
10. Internet tools — search, fetch, summarize
11. Automation — scheduler, workflows, watchers
12. Plugins — loader, SDK, docs
13. Frontend — React chat UI, settings, memory viewer
14. Desktop — Electron wrapper
15. Testing — coverage pass
16. Docker
17. Deployment
18. Optimization — caching, async audit
19. Security review
20. Final documentation

## Current status

Phase 6 complete. Agent loop streams; two providers behind the ABC
(Anthropic + Ollama), switchable via LLM_PROVIDER in .env. Memory is live:
conversations resume across reconnects, facts are extracted after each turn
into SQLite, and Chroma retrieves them by meaning. Tool calling and the
confirmation gate are deferred to Phase 9, when tools exist.

Known limitation: fact extraction quality tracks model size. llama3.2 (3B)
is inconsistent -- the same exchange can yield facts on one run and nothing
on the next. Switch LLM_PROVIDER to anthropic for reliable extraction.

Phase 7a complete: STTProvider and TTSProvider behind ABCs, faster-whisper
and Piper adapters, POST /voice/transcribe and /voice/speak. Whisper
hallucinations are filtered via VAD plus no_speech_prob / avg_logprob.
Measured ~2x realtime transcription on 4 CPU cores.

Phase 7b complete: the voice client in voice/ -- microphone capture via
sounddevice, energy-based endpointing calibrated to room noise,
push-to-talk, and "hey jarvis" via openWakeWord. It lives outside backend/
because capturing a microphone is an interface concern, and it imports
nothing from backend/app -- proving the public API is enough to drive the
whole assistant, which Phase 13's UI will also need.

Phase 8 complete: VisionProvider and OCRProvider behind ABCs (Anthropic +
Ollama vision, RapidOCR), screen capture via mss, and /vision endpoints.
VisionProvider is async and OCRProvider is sync, following the same rule as
elsewhere: network waits are async, local computation is not.

Measured on this hardware: OCR reads a real screenshot in 4.5s locally and
free; moondream takes ~20s and is too weak for screenshots (it called a
desktop "irc"). Use OCR for screen text and Claude for genuine image
understanding.

Phase 9a complete: the Tool interface, the path sandbox, the confirmation
gate, audit logging, and five filesystem tools behind /tools endpoints.
Three safety layers -- containment inside FS_ROOT (resolved BEFORE checking,
so '..' and symlinks cannot escape), a deny-list for ~/.ssh and .env style
paths inside it, and the single gate in core/gate.py that every execution
passes through. Audit rows are committed before the tool runs, so a crash
mid-execution still leaves evidence.

Phase 9b complete: the model calls tools itself. LLMProvider gained
stream_turn() as a CONCRETE method (not abstract) whose default adapts
stream_chat and ignores tools -- so every provider written before tools
existed keeps working. Anthropic and Ollama both override it; the shapes
are entirely different (typed content blocks vs an OpenAI-style function
array with no call ids), which is exactly the reshaping an adapter is for.

A destructive tool ENDS the turn with a confirmation request rather than
suspending the loop, so no database session or provider connection is held
while a human decides. Approval runs the tool and records the result into
the conversation; it deliberately does not re-prompt the model, which would
cost a round trip per confirmation.

Tools gained a ToolContext (db, user_id, memory), added while there were
still only five of them. Two new tools: get_current_time and remember_fact.
The gate now logs status="unknown_tool" when the model asks for something
that does not exist -- a feature request generated by real use.

Phase 10 complete: web_search (ddgs, no API key) and fetch_url
(trafilatura extraction) as read-only Tools, plus tools/urls.py -- the
network equivalent of paths.py. It resolves a hostname to real addresses
BEFORE checking them, because public domains like localtest.me resolve to
127.0.0.1 and a string check on "localhost" stops nothing.

This is the first phase where stranger-written text reaches a model holding
destructive tools. Fetched content is labelled untrusted, but the labelling
is a nudge -- the guarantee is the Phase 9a gate, and there is a test that
assumes the model IS fooled and asserts nothing is destroyed anyway.

Phase 11a complete: APScheduler, a reminders table, and create/list/cancel
reminder tools. ONE polling job asks the database every 20s whether anything
is due, rather than a timer per reminder -- less precise, but the scheduler
holds no state and survives restarts, sleeps and crashes with no recovery
code. A reminder is marked delivered only once a client actually receives
it, so one that came due while JARVIS was closed arrives on next connect
instead of vanishing. Times are naive UTC internally (SQLite drops tzinfo)
and converted at the edges.

Two live bugs found and fixed during this phase:
  - The agent loop broke out of the tool loop on the first confirmation,
    silently dropping every later tool in the same turn. Observed: Claude
    asked for delete_file and create_reminder together, the delete needed
    approval, and the reminder was never set with nothing said. Now every
    call is accounted for -- safe ones run, dangerous ones each ask.
  - create_reminder accepted only an ISO timestamp, on the theory that the
    model converts natural language. True for absolute times, false for
    relative ones: asked to remind "in 40 seconds", Claude sent
    due_at="40 seconds". due_in_seconds was added.

Phase 11b not started: file watchers and multi-step workflows.

Update this line at the end of every phase.
