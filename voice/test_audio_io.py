"""
Tests for the audio helpers.

Run from this folder:
    ..\\.venv\\Scripts\\python.exe -m pytest

These cover the maths and the WAV container only. Nothing here opens a
microphone or a speaker -- device I/O cannot be asserted on in CI, and the
parts that actually go wrong are the calculations.
"""

import io
import wave

import numpy as np
import pytest

import audio_io


def test_rms_of_silence_is_zero():
    assert audio_io.rms(np.zeros(1000, dtype=np.int16)) == 0.0


def test_rms_grows_with_loudness():
    quiet = audio_io.rms(np.full(1000, 100, dtype=np.int16))
    loud = audio_io.rms(np.full(1000, 5000, dtype=np.int16))

    assert loud > quiet


def test_rms_ignores_the_sign_of_the_waveform():
    """
    Sound waves swing above and below zero, so a plain average would cancel
    out to nothing. Squaring before averaging is what stops that -- this
    test is here to catch anyone "simplifying" it back to a mean.
    """
    wave_samples = np.array([3000, -3000] * 500, dtype=np.int16)

    assert audio_io.rms(wave_samples) == pytest.approx(3000, rel=0.01)


def test_rms_of_an_empty_array_does_not_crash():
    # np.mean of an empty array is NaN and warns; the guard must come first.
    assert audio_io.rms(np.array([], dtype=np.int16)) == 0.0


def test_to_wav_bytes_produces_a_real_readable_wav():
    samples = np.zeros(audio_io.SAMPLE_RATE, dtype=np.int16)  # one second

    data = audio_io.to_wav_bytes(samples)

    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WAVE"

    with wave.open(io.BytesIO(data)) as wav_file:
        assert wav_file.getframerate() == audio_io.SAMPLE_RATE
        assert wav_file.getnchannels() == audio_io.CHANNELS
        assert wav_file.getsampwidth() == 2  # int16
        assert wav_file.getnframes() == audio_io.SAMPLE_RATE


def test_wav_round_trip_preserves_the_samples():
    original = np.array([0, 1000, -1000, 32000, -32000], dtype=np.int16)

    data = audio_io.to_wav_bytes(original)
    with wave.open(io.BytesIO(data)) as wav_file:
        restored = np.frombuffer(
            wav_file.readframes(wav_file.getnframes()), dtype=np.int16
        )

    assert np.array_equal(restored, original)


def test_sample_rate_is_16k():
    """
    Not arbitrary: Whisper and openWakeWord both expect 16kHz mono. Changing
    this breaks transcription accuracy and the wake word outright, so it is
    pinned deliberately rather than by accident.
    """
    assert audio_io.SAMPLE_RATE == 16_000
    assert audio_io.CHANNELS == 1
