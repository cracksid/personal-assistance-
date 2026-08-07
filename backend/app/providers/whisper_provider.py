"""
Speech-to-text using faster-whisper.

faster-whisper is a reimplementation of OpenAI's Whisper on CTranslate2. It
runs several times quicker than the reference implementation on a CPU and
uses far less memory, which is what makes local transcription practical on a
laptop with no GPU (CLAUDE.md's Windows notes pick it for exactly this).

This is the only module that knows Whisper exists.
"""

import io
import logging

from faster_whisper import WhisperModel

from app.config import settings
from app.providers.speech import STTProvider, SpeechProviderError, Transcript

logger = logging.getLogger(__name__)


class WhisperSTTProvider(STTProvider):
    """Transcribes audio locally with faster-whisper."""

    def __init__(self) -> None:
        self._model: WhisperModel | None = None
        logger.info(
            "Whisper STT ready (model=%s, device=%s, compute=%s)",
            settings.stt_model,
            settings.stt_device,
            settings.stt_compute_type,
        )

    def _get_model(self) -> WhisperModel:
        """
        Load the model on first use, not in __init__.

        Loading downloads several hundred megabytes the first time and then
        holds the weights in memory. Doing that during dependency injection
        would stall the first request that touches voice, and would make the
        object impossible to construct on a machine that is offline.
        """
        if self._model is None:
            logger.info("Loading Whisper model %r (first use may download it)", settings.stt_model)
            self._model = WhisperModel(
                settings.stt_model,
                device=settings.stt_device,
                compute_type=settings.stt_compute_type,
            )
        return self._model

    def transcribe(self, audio: bytes) -> Transcript:
        if not audio:
            raise SpeechProviderError("No audio was supplied.")

        model = self._get_model()

        try:
            # vad_filter runs Silero voice-activity detection first, cutting
            # silence out before transcription. This is the single most
            # effective defence against Whisper inventing text: given nothing
            # to transcribe, it has nothing to hallucinate from.
            segments, info = model.transcribe(
                io.BytesIO(audio),
                vad_filter=True,
                beam_size=5,
            )

            # `segments` is a generator -- transcription only actually runs as
            # it is consumed, so this list() is where the work happens.
            found = list(segments)

        except Exception as exc:
            raise SpeechProviderError(f"Transcription failed: {exc}") from exc

        if not found:
            # VAD found no speech at all. A real, common outcome: the user
            # pressed the key and said nothing.
            return Transcript(text="", language=info.language)

        text = " ".join(segment.text.strip() for segment in found).strip()

        # Average the per-segment confidences into one signal for the clip.
        no_speech = sum(s.no_speech_prob for s in found) / len(found)
        logprob = sum(s.avg_logprob for s in found) / len(found)

        if self._looks_like_a_hallucination(text, no_speech, logprob):
            logger.info(
                "Discarding likely hallucination (no_speech=%.2f logprob=%.2f): %r",
                no_speech,
                logprob,
                text[:80],
            )
            return Transcript(
                text="", language=info.language, no_speech_prob=no_speech, avg_logprob=logprob
            )

        return Transcript(
            text=text,
            language=info.language,
            no_speech_prob=no_speech,
            avg_logprob=logprob,
        )

    @staticmethod
    def _looks_like_a_hallucination(text: str, no_speech: float, logprob: float) -> bool:
        """
        Decide whether a transcript is Whisper talking to itself.

        Whisper produces fluent, confident-sounding text from silence and
        background noise -- "Thank you for watching!" from a silent clip is
        the canonical example, learned from subtitle training data. Passing
        that into the agent loop means acting on commands nobody gave.

        Two independent signals, either of which is enough to reject:
          - the model says the audio probably contains no speech
          - the model was guessing at the words it chose
        """
        if not text:
            return False  # already empty; nothing to reject
        if no_speech > settings.stt_no_speech_threshold:
            return True
        if logprob < settings.stt_logprob_threshold:
            return True
        return False
