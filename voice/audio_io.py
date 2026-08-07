"""
Microphone capture and speaker playback.

Everything here deals with raw audio samples. It knows nothing about JARVIS,
HTTP, or wake words -- which keeps client.py readable as a sequence of steps
rather than a pile of buffer arithmetic.

Audio is captured at 16kHz mono, because that is what Whisper and
openWakeWord both expect. Your microphone almost certainly runs at 44.1kHz
natively; sounddevice asks Windows to resample, so we never have to.
"""

import io
import wave

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16_000
CHANNELS = 1

# 100ms of audio. Small enough that the silence detector reacts promptly,
# large enough that we are not doing arithmetic thousands of times a second.
CHUNK_SAMPLES = SAMPLE_RATE // 10


def rms(samples: np.ndarray) -> float:
    """
    Loudness of a chunk, as root-mean-square amplitude.

    Squaring makes every sample positive (sound waves swing both ways, so a
    plain average would cancel to roughly zero); the mean gives average
    energy; the square root brings it back to the scale of the original
    samples. It is the standard cheap "how loud is this" measure.
    """
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


def measure_ambient_noise(seconds: float = 0.6) -> float:
    """
    Listen to the room briefly to learn what "quiet" sounds like here.

    A hardcoded silence threshold cannot work: a quiet room, a laptop fan,
    and a café differ by orders of magnitude. Calibrating on startup means
    the same code behaves sensibly in all of them.
    """
    recording = sd.rec(
        int(seconds * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16",
    )
    sd.wait()
    return rms(recording)


def record_until_silence(
    ambient: float,
    max_seconds: float = 15.0,
    silence_seconds: float = 1.2,
    min_seconds: float = 0.4,
) -> bytes:
    """
    Record from the microphone until the speaker stops, returning WAV bytes.

    This is endpointing: deciding when an utterance has finished. Recording
    for a fixed duration would either cut people off or make them wait, so
    instead we watch the loudness and stop once it has been near the ambient
    level for `silence_seconds`.

    `min_seconds` stops the pause *before* someone starts speaking from
    ending the recording immediately.

    Args:
        ambient: the room's noise floor, from measure_ambient_noise().
    """
    # Speech has to clear the noise floor by a clear margin to count. The
    # additive floor matters for a very quiet room, where ambient can be
    # near zero and a pure multiple would trigger on nothing.
    speech_threshold = max(ambient * 3.0, 120.0)

    chunks: list[np.ndarray] = []
    silent_chunks = 0
    chunk_seconds = CHUNK_SAMPLES / SAMPLE_RATE
    required_silent = int(silence_seconds / chunk_seconds)
    max_chunks = int(max_seconds / chunk_seconds)

    with sd.InputStream(
        samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16"
    ) as stream:
        while len(chunks) < max_chunks:
            block, _overflowed = stream.read(CHUNK_SAMPLES)
            chunks.append(block.copy())

            if rms(block) < speech_threshold:
                silent_chunks += 1
            else:
                silent_chunks = 0

            elapsed = len(chunks) * chunk_seconds
            if elapsed >= min_seconds and silent_chunks >= required_silent:
                break

    return to_wav_bytes(np.concatenate(chunks) if chunks else np.zeros((0, 1), "int16"))


def to_wav_bytes(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    """
    Wrap raw samples in a WAV container, in memory.

    The API wants a real audio file, not a bare array -- a WAV header is
    what tells the far end the sample rate, channel count and bit depth.
    """
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(2)  # int16 == 2 bytes per sample
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(samples.tobytes())
    return buffer.getvalue()


def play_wav(wav_bytes: bytes) -> None:
    """Play a WAV file through the default output device, blocking until done."""
    with wave.open(io.BytesIO(wav_bytes)) as wav_file:
        rate = wav_file.getframerate()
        frames = wav_file.readframes(wav_file.getnframes())

    samples = np.frombuffer(frames, dtype=np.int16)
    sd.play(samples, samplerate=rate)
    sd.wait()
