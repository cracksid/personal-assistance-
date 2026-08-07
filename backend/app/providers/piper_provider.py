"""
Text-to-speech using Piper.

Piper is a local neural TTS engine: it runs on the CPU, needs no account or
network, and sounds markedly more natural than the speech synthesis built
into Windows. A voice is two files -- an ONNX model and a JSON config --
downloaded once with:

    python -m piper.download_voices en_GB-alan-medium --download-dir models/piper

This is the only module that knows Piper exists.
"""

import io
import logging
import wave
from pathlib import Path

from piper import PiperVoice, SynthesisConfig

from app.config import settings
from app.providers.speech import SpeechProviderError, TTSProvider

logger = logging.getLogger(__name__)


class PiperTTSProvider(TTSProvider):
    """Speaks text locally with Piper."""

    def __init__(self) -> None:
        # pathlib, not string concatenation -- CLAUDE.md, and this is Windows.
        self._model_path = Path(settings.tts_model_dir) / f"{settings.tts_voice}.onnx"
        self._voice: PiperVoice | None = None
        logger.info("Piper TTS ready (voice=%s)", settings.tts_voice)

    def _get_voice(self) -> PiperVoice:
        """
        Load the voice on first use.

        Same reasoning as the Whisper adapter: loading reads ~60MB from disk
        into memory, which should not happen while a dependency is being
        injected, and a missing file should surface as a readable error to
        the caller rather than killing the request before it starts.
        """
        if self._voice is None:
            if not self._model_path.exists():
                raise SpeechProviderError(
                    f"Voice model not found at {self._model_path}. Download it with:  "
                    f"python -m piper.download_voices {settings.tts_voice} "
                    f"--download-dir {settings.tts_model_dir}"
                )
            logger.info("Loading Piper voice from %s", self._model_path)
            self._voice = PiperVoice.load(self._model_path)
        return self._voice

    def synthesize(self, text: str) -> bytes:
        text = text.strip()
        if not text:
            raise SpeechProviderError("No text was supplied.")

        voice = self._get_voice()

        # Piper's knob is "length_scale": how long to stretch each phoneme,
        # so SMALLER means faster. That inversion is a trap in a config file,
        # so .env exposes TTS_SPEED where larger means faster, and the
        # reciprocal is taken here.
        config = SynthesisConfig(length_scale=1.0 / settings.tts_speed)

        try:
            # Synthesise straight into memory. BytesIO behaves like a file,
            # and the wave module writes a proper WAV header into it, so the
            # result is a complete playable file that never touched the disk.
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as wav_file:
                voice.synthesize_wav(text, wav_file, syn_config=config)
            return buffer.getvalue()

        except SpeechProviderError:
            raise
        except Exception as exc:
            raise SpeechProviderError(f"Speech synthesis failed: {exc}") from exc
