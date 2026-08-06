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

Phase 3 complete. Phase 4 not started.

Update this line at the end of every phase.
