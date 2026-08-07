"""
Tests for the speech providers and the /voice endpoints.

None of these load a model or run inference. Whisper takes seconds per clip
and Piper holds 60MB of weights, so the real engines are replaced with fakes
and the tests stay in milliseconds. What is under test here is our code --
request handling, error translation, and the hallucination guard -- not
whether Whisper can hear.
"""

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_stt, get_tts
from app.main import app
from app.providers.speech import (
    STTProvider,
    SpeechProviderError,
    Transcript,
    TTSProvider,
)
from app.providers.whisper_provider import WhisperSTTProvider

# A minimal but real WAV file: 44-byte header plus a little silence.
FAKE_WAV = b"RIFF$\x00\x00\x00WAVEfmt " + b"\x00" * 36


class FakeSTT(STTProvider):
    def __init__(self, text: str = "hello jarvis") -> None:
        self.text = text
        self.received: bytes = b""

    def transcribe(self, audio: bytes) -> Transcript:
        self.received = audio
        return Transcript(text=self.text, language="en")


class FakeTTS(TTSProvider):
    def __init__(self, audio: bytes = FAKE_WAV) -> None:
        self.audio = audio
        self.received: str = ""

    def synthesize(self, text: str) -> bytes:
        self.received = text
        return self.audio


class BrokenSTT(STTProvider):
    def transcribe(self, audio: bytes) -> Transcript:
        raise SpeechProviderError("model file is missing")


class BrokenTTS(TTSProvider):
    def synthesize(self, text: str) -> bytes:
        raise SpeechProviderError("voice model is missing")


@pytest.fixture
def stt() -> FakeSTT:
    return FakeSTT()


@pytest.fixture
def tts() -> FakeTTS:
    return FakeTTS()


@pytest.fixture
def client(stt: FakeSTT, tts: FakeTTS) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_stt] = lambda: stt
    app.dependency_overrides[get_tts] = lambda: tts
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# --- the abstractions ------------------------------------------------------


def test_incomplete_speech_providers_cannot_be_instantiated():
    """
    Same guarantee as LLMProvider: a subclass that forgets a required method
    fails at construction, not deep inside a request.
    """

    class NoTranscribe(STTProvider):
        pass

    class NoSynthesize(TTSProvider):
        pass

    with pytest.raises(TypeError):
        NoTranscribe()
    with pytest.raises(TypeError):
        NoSynthesize()


# --- the hallucination guard ----------------------------------------------


@pytest.mark.parametrize(
    "text,no_speech,logprob,expected,why",
    [
        ("Thank you for watching!", 0.95, -0.2, True, "model says it is silence"),
        ("mmm hmm shhh", 0.1, -1.8, True, "model was guessing at the words"),
        ("what is on my calendar", 0.01, -0.35, False, "confident real speech"),
        ("", 0.99, -3.0, False, "already empty; nothing to reject"),
    ],
)
def test_hallucination_guard(text, no_speech, logprob, expected, why):
    """
    Whisper invents fluent text from silence -- "Thank you for watching!" is
    the canonical example, learned from subtitle training data. Acting on
    that means obeying commands nobody gave, so the guard rejects a
    transcript whenever Whisper's own confidence signals say it was not
    really listening to speech.
    """
    assert (
        WhisperSTTProvider._looks_like_a_hallucination(text, no_speech, logprob)
        is expected
    ), why


# --- POST /voice/transcribe ------------------------------------------------


def test_transcribe_returns_text(client: TestClient, stt: FakeSTT):
    response = client.post("/voice/transcribe", content=FAKE_WAV)

    assert response.status_code == 200
    assert response.json()["text"] == "hello jarvis"
    assert stt.received == FAKE_WAV  # the bytes reached the engine unchanged


def test_transcribe_rejects_an_empty_body(client: TestClient):
    response = client.post("/voice/transcribe", content=b"")

    assert response.status_code == 400


def test_transcribe_rejects_oversized_audio(client: TestClient):
    from app.api.routes.voice import MAX_AUDIO_BYTES

    response = client.post("/voice/transcribe", content=b"\x00" * (MAX_AUDIO_BYTES + 1))

    assert response.status_code == 413


def test_transcribe_translates_engine_failure_into_a_readable_error(tts: FakeTTS):
    app.dependency_overrides[get_stt] = lambda: BrokenSTT()
    app.dependency_overrides[get_tts] = lambda: tts
    try:
        response = TestClient(app).post("/voice/transcribe", content=FAKE_WAV)

        assert response.status_code == 422
        assert "model file is missing" in response.json()["error"]
    finally:
        app.dependency_overrides.clear()


def test_silence_transcribes_to_empty_text_not_an_error(tts: FakeTTS):
    """
    Saying nothing is a normal outcome, not a failure. The caller gets 200
    with an empty string and simply has nothing to act on.
    """
    app.dependency_overrides[get_stt] = lambda: FakeSTT(text="")
    app.dependency_overrides[get_tts] = lambda: tts
    try:
        response = TestClient(app).post("/voice/transcribe", content=FAKE_WAV)

        assert response.status_code == 200
        assert response.json()["text"] == ""
    finally:
        app.dependency_overrides.clear()


# --- POST /voice/speak -----------------------------------------------------


def test_speak_returns_playable_wav(client: TestClient, tts: FakeTTS):
    response = client.post("/voice/speak", json={"text": "Good evening, Sid."})

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content == FAKE_WAV
    assert tts.received == "Good evening, Sid."


def test_speak_rejects_empty_text(client: TestClient):
    # Rejected by the Pydantic model before any engine is touched.
    response = client.post("/voice/speak", json={"text": ""})

    assert response.status_code == 422


def test_speak_translates_engine_failure_into_a_readable_error(stt: FakeSTT):
    app.dependency_overrides[get_stt] = lambda: stt
    app.dependency_overrides[get_tts] = lambda: BrokenTTS()
    try:
        response = TestClient(app).post("/voice/speak", json={"text": "hello"})

        assert response.status_code == 422
        assert "voice model is missing" in response.json()["error"]
    finally:
        app.dependency_overrides.clear()
