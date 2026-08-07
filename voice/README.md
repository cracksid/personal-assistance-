# JARVIS voice client

Talk to JARVIS out loud. One turn:

```
microphone -> POST /voice/transcribe -> WS /ws/chat -> POST /voice/speak -> speaker
```

## Setup

The backend must be running first:

```bash
cd backend
python -m uvicorn app.main:app
```

Install this client's dependencies into the same venv (once):

```bash
.venv\Scripts\python.exe -m pip install -r voice/requirements.txt
```

Download the wake word models (once, ~5MB):

```bash
.venv\Scripts\python.exe -c "from openwakeword import utils; utils.download_models(model_names=['hey_jarvis'])"
```

## Run

```bash
.venv\Scripts\python.exe voice/client.py
```

Press Enter, speak, then pause. It stops recording on its own when you stop
talking, transcribes, sends it through the agent loop, and reads the answer
back to you.

Hands-free instead:

```bash
.venv\Scripts\python.exe voice/client.py --wake
```

Say **"hey jarvis"**, wait for the prompt, then speak.

Point it at a different port with `JARVIS_HOST=127.0.0.1:8001`.

## Tests

```bash
cd voice
..\.venv\Scripts\python.exe -m pytest
```

These cover the audio maths and the WAV container. Nothing opens a microphone
— device I/O can't be asserted on, and the calculations are what actually
break.

## How it decides you've finished speaking

Recording for a fixed number of seconds either cuts people off or makes them
wait, so the client watches loudness instead and stops after ~1.2s of quiet.

The threshold is calibrated at startup by listening to the room for half a
second. A hardcoded number cannot work: a quiet bedroom, a laptop fan and a
café differ by orders of magnitude.

## Why this lives outside `backend/`

Capturing your microphone is an interface concern — the top tier of the
architecture, alongside `frontend/` and `desktop/`. This client imports
nothing from `backend/app`; it only speaks HTTP and WebSocket.

That restriction is a test. If a voice client can drive the whole assistant
through the public API alone, Phase 13's browser UI can too.

## Notes

- **Wake word threshold** is 0.5 in `client.py`. Lower triggers more easily
  (including on the television); higher makes you repeat yourself. Measured
  with synthesised speech: "hey jarvis" scores 0.998, "what is the time"
  scores 0.000.
- **The detector is paused while JARVIS speaks**, otherwise it hears its own
  voice through the speakers and triggers on itself.
- **Transcription runs at roughly 2x realtime** on a 4-core CPU with no GPU,
  so expect a few seconds' pause after you stop talking. `STT_MODEL=base` in
  `.env` roughly halves it, at a real cost to accuracy on names.
