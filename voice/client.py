"""
The JARVIS voice client.

Run it:
    .venv\\Scripts\\python.exe voice/client.py              # push to talk
    .venv\\Scripts\\python.exe voice/client.py --wake       # "hey jarvis"

One full turn:

    microphone -> POST /voice/transcribe -> WS /ws/chat -> POST /voice/speak
                                                                    -> speaker

This is an INTERFACE, in the top tier of the architecture -- the same layer
as the React UI and the Electron shell. It imports nothing from backend/app
and talks to JARVIS only over HTTP and WebSocket.

That restriction is deliberate. If a voice client can drive the whole
assistant through the public API alone, Phase 13's browser UI can too. If it
could not, the API would have a gap worth finding now rather than later.
"""

import argparse
import asyncio
import json
import os
import sys

import httpx
import websockets

import audio_io

# Override with JARVIS_HOST if the server runs on a different port, e.g.
#   set JARVIS_HOST=127.0.0.1:8001
HOST = os.environ.get("JARVIS_HOST", "127.0.0.1:8000")
BASE_URL = f"http://{HOST}"
CHAT_URL = f"ws://{HOST}/ws/chat"

# How confident openWakeWord must be before we treat it as "hey jarvis".
# Lower triggers on more things (including the television); higher makes you
# repeat yourself. 0.5 is the library's own suggested starting point.
WAKE_THRESHOLD = 0.5

# openWakeWord expects exactly 80ms of 16kHz audio per call.
WAKE_CHUNK = 1280


async def transcribe(client: httpx.AsyncClient, wav: bytes) -> str:
    """Send recorded audio to JARVIS and get back what it heard."""
    response = await client.post(
        "/voice/transcribe", content=wav, headers={"Content-Type": "audio/wav"}
    )
    response.raise_for_status()
    return response.json()["text"].strip()


async def ask(text: str) -> str:
    """
    Put one message through the agent loop and collect the whole reply.

    A fresh connection per turn keeps this simple. The conversation still
    continues, because the server resumes the most recent conversation
    rather than starting a new one (Phase 6).
    """
    reply: list[str] = []
    async with websockets.connect(CHAT_URL, max_size=None) as socket:
        await socket.send(text)
        while True:
            frame = json.loads(await socket.recv())
            if frame["type"] == "chunk":
                reply.append(frame["text"])
            elif frame["type"] == "done":
                break
            elif frame["type"] == "error":
                return f"Sorry, something went wrong. {frame['message']}"
    return "".join(reply).strip()


async def speak(client: httpx.AsyncClient, text: str) -> None:
    """Have JARVIS say something out loud."""
    response = await client.post("/voice/speak", json={"text": text})
    response.raise_for_status()
    # Playback blocks until the audio finishes, so it goes on a worker thread
    # to keep the event loop free.
    await asyncio.to_thread(audio_io.play_wav, response.content)


async def handle_one_turn(client: httpx.AsyncClient, ambient: float) -> None:
    """Record, transcribe, think, and reply out loud."""
    print("  listening... (speak, then pause)")
    wav = await asyncio.to_thread(audio_io.record_until_silence, ambient)

    heard = await transcribe(client, wav)
    if not heard:
        # Either silence, or a transcript the server rejected as a likely
        # Whisper hallucination. Both mean "nothing was said".
        print("  (heard nothing)")
        return

    print(f"  you: {heard}")
    reply = await ask(heard)
    print(f"  jarvis: {reply}")

    if reply:
        await speak(client, reply)


async def push_to_talk(client: httpx.AsyncClient, ambient: float) -> None:
    """Press Enter to speak. Simple, and it never mishears a trigger."""
    print("\nPush-to-talk. Press Enter to speak, Ctrl+C to quit.\n")
    while True:
        await asyncio.to_thread(input, "[Enter to speak] ")
        await handle_one_turn(client, ambient)


async def wake_word(client: httpx.AsyncClient, ambient: float) -> None:
    """Listen continuously for "hey jarvis", then handle a turn."""
    from openwakeword.model import Model

    print("\nLoading the wake word model...")
    # onnx rather than tflite: onnxruntime is already installed (Chroma
    # brought it in), and tflite-runtime has no Windows wheels.
    detector = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")

    import sounddevice as sd

    print('Listening for "hey jarvis". Ctrl+C to quit.\n')
    with sd.InputStream(
        samplerate=audio_io.SAMPLE_RATE, channels=1, dtype="int16"
    ) as stream:
        while True:
            block, _ = await asyncio.to_thread(stream.read, WAKE_CHUNK)
            scores = await asyncio.to_thread(detector.predict, block.flatten())

            if scores.get("hey_jarvis", 0.0) > WAKE_THRESHOLD:
                print(f"  [heard: hey jarvis  ({scores['hey_jarvis']:.2f})]")

                # Stop feeding the detector while we record and reply, or it
                # would hear JARVIS's own voice through the speakers and
                # trigger on itself.
                stream.stop()
                try:
                    await handle_one_turn(client, ambient)
                finally:
                    detector.reset()  # clear the buffer so it can't re-fire
                    stream.start()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Talk to JARVIS.")
    parser.add_argument(
        "--wake",
        action="store_true",
        help='listen for "hey jarvis" instead of waiting for Enter',
    )
    args = parser.parse_args()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=120.0) as client:
        try:
            await client.get("/health")
        except httpx.ConnectError:
            print(f"Cannot reach JARVIS at {BASE_URL}.")
            print("Start it first:  cd backend && python -m uvicorn app.main:app")
            sys.exit(1)

        print("Calibrating to room noise, stay quiet for a moment...")
        ambient = await asyncio.to_thread(audio_io.measure_ambient_noise)
        print(f"  noise floor: {ambient:.0f}")

        if args.wake:
            await wake_word(client, ambient)
        else:
            await push_to_talk(client, ambient)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGoodbye.")
