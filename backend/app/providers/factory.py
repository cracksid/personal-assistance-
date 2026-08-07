"""
Chooses which provider adapter to build, based on config.

This is the ONE place in the codebase where a provider name appears in code.
core/ imports the abstract LLMProvider and calls get_llm_provider(); it never
names a vendor. Adding Ollama or OpenAI later means writing a new adapter and
adding one line to the dict below -- nothing in core/ changes.

You can audit the rule with a search: "AnthropicProvider" should appear in
exactly two files -- its own, and this one.
"""

from app.config import settings
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import LLMProvider, LLMProviderError
from app.providers.ollama_provider import OllamaProvider
from app.providers.piper_provider import PiperTTSProvider
from app.providers.speech import STTProvider, TTSProvider
from app.providers.whisper_provider import WhisperSTTProvider

# Maps the LLM_PROVIDER value in .env to the class that implements it.
# The values are the classes themselves, not instances -- we build one only
# when it's actually asked for.
#
# Adding Ollama to a working Anthropic setup cost exactly two lines here plus
# one new adapter file. app/core/agent.py was not touched -- that is the whole
# point of the LLMProvider abstract base class.
_PROVIDERS: dict[str, type[LLMProvider]] = {
    "anthropic": AnthropicProvider,
    "ollama": OllamaProvider,
}


def get_llm_provider() -> LLMProvider:
    """
    Build the provider named by settings.llm_provider.

    Note the return type: LLMProvider, not AnthropicProvider. Callers get the
    abstract type, so their code cannot accidentally depend on anything
    Claude-specific.
    """
    provider_class = _PROVIDERS.get(settings.llm_provider)
    if provider_class is None:
        known = ", ".join(sorted(_PROVIDERS))
        raise LLMProviderError(
            f"Unknown LLM_PROVIDER {settings.llm_provider!r}. Known providers: {known}."
        )
    return provider_class()


# Speech engines follow exactly the same pattern: the name lives in .env,
# the mapping lives here, and nothing above this file names an engine.
_STT_PROVIDERS: dict[str, type[STTProvider]] = {
    "whisper": WhisperSTTProvider,
}

_TTS_PROVIDERS: dict[str, type[TTSProvider]] = {
    "piper": PiperTTSProvider,
}


def get_stt_provider() -> STTProvider:
    """Build the speech-to-text engine named by settings.stt_provider."""
    provider_class = _STT_PROVIDERS.get(settings.stt_provider)
    if provider_class is None:
        known = ", ".join(sorted(_STT_PROVIDERS))
        raise LLMProviderError(
            f"Unknown STT_PROVIDER {settings.stt_provider!r}. Known providers: {known}."
        )
    return provider_class()


def get_tts_provider() -> TTSProvider:
    """Build the text-to-speech engine named by settings.tts_provider."""
    provider_class = _TTS_PROVIDERS.get(settings.tts_provider)
    if provider_class is None:
        known = ", ".join(sorted(_TTS_PROVIDERS))
        raise LLMProviderError(
            f"Unknown TTS_PROVIDER {settings.tts_provider!r}. Known providers: {known}."
        )
    return provider_class()
