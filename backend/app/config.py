"""
Application configuration.

Settings are declared once here as a typed class, then loaded from environment
variables and the .env file. Nothing else in the codebase should read
os.environ directly -- always go through the `settings` object this module
creates, so there's exactly one place that knows where config comes from.
"""

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# __file__ is this file's own path. .resolve() makes it absolute, then each
# .parent goes up one folder: config.py -> app/ -> backend/
# Using pathlib (not string concatenation) is a hard rule in CLAUDE.md --
# it gets Windows path separators right without any manual escaping.
BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent
DEFAULT_DB_PATH = BACKEND_DIR / "jarvis.db"

# .env lives at the project root, next to .gitignore.
#
# This MUST be an absolute path. A relative "​.env" is resolved against the
# current working directory, so it would only be found when the server happens
# to be started from the project root -- and we start it from backend/. The
# failure is silent: pydantic-settings finds no file, falls back to every
# default, and the app runs with settings you never chose.
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """
    Each attribute below is a config value. The type hint (str, int, bool...)
    tells Pydantic what type to convert the raw environment variable into,
    and gives you autocomplete + validation everywhere `settings` is used.
    """

    app_name: str = "JARVIS"
    environment: str = "development"  # "development" or "production"
    host: str = "127.0.0.1"  # bind to localhost only -- see CLAUDE.md auth decision
    port: int = 8000
    log_level: str = "INFO"

    # SQLAlchemy connection string. The "sqlite:///" prefix picks the database
    # driver; everything after it is the path to the file. .as_posix() writes
    # the path with forward slashes, which is the format URLs expect even on
    # Windows. Swapping to Postgres later means changing only this one line
    # (see the locked decisions in CLAUDE.md).
    database_url: str = f"sqlite:///{DEFAULT_DB_PATH.as_posix()}"

    # --- LLM ---------------------------------------------------------------
    # Which provider adapter to load. Nothing in core/ ever reads this value --
    # only providers/factory.py does. That is what keeps the agent loop from
    # knowing which model it is talking to (see CLAUDE.md).
    llm_provider: str = "anthropic"

    llm_model: str = "claude-opus-5"

    # How hard the model works before answering. "low" is fastest and cheapest,
    # "max" is the most thorough. See .env.example for the full ladder.
    llm_effort: str = "medium"

    # SecretStr is a string wrapper whose repr() prints "**********" instead of
    # the value. That means printing or logging the settings object -- by
    # accident or otherwise -- can never leak the key. Reading the real value
    # requires an explicit .get_secret_value() call, which is easy to spot in
    # review. CLAUDE.md: "Never log a key."
    anthropic_api_key: SecretStr = SecretStr("")

    # --- Ollama (local models; only used when LLM_PROVIDER=ollama) ----------
    # No API key: Ollama runs on your own machine, so nothing leaves it and
    # nothing is billed.
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"

    # --- Speech to text ----------------------------------------------------
    stt_provider: str = "whisper"

    # Whisper model size: tiny | base | small | medium | large-v3.
    # "small" is the accuracy/speed sweet spot without a GPU. Downloaded
    # automatically on first use (~485MB) and cached afterwards.
    stt_model: str = "small"

    # "cpu" or "cuda". int8 quantisation roughly halves memory and speeds up
    # CPU inference for a small accuracy cost -- the right trade on a laptop.
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"

    # Hallucination guards. Whisper invents text from silence, so a
    # transcript is discarded when the model itself signals low confidence.
    # no_speech_prob near 1.0 means "this is silence"; avg_logprob strongly
    # negative means the model was guessing.
    stt_no_speech_threshold: float = 0.6
    stt_logprob_threshold: float = -1.0

    # --- Text to speech ----------------------------------------------------
    tts_provider: str = "piper"

    # Voice model name. Download others with:
    #   python -m piper.download_voices <name> --download-dir backend/models/piper
    # Browse them at https://rhasspy.github.io/piper-samples/
    tts_voice: str = "en_GB-alan-medium"
    tts_model_dir: str = str(BACKEND_DIR / "models" / "piper")

    # --- Memory ------------------------------------------------------------
    # Where Chroma keeps the vector index on disk, alongside jarvis.db.
    # Gitignored -- it is derived data, rebuildable from the facts table.
    chroma_dir: str = str(BACKEND_DIR / "chroma_data")

    # How many remembered facts to pull into the prompt each turn. Every fact
    # costs tokens, so this is a deliberate cap rather than "everything".
    memory_search_limit: int = 5

    # Maximum cosine distance for a fact to count as relevant (0 = identical,
    # 2 = opposite). Without this, a small fact store returns every fact for
    # every query, which measurably breaks fact extraction. Sits in the gap
    # between relevant and irrelevant measured on real queries -- see
    # memory/store.py -> search().
    memory_relevance_cutoff: float = 0.78

    # This tells pydantic-settings *how* to load the values above: read the
    # .env file at the absolute path computed at the top of this module, using
    # UTF-8 text encoding. If a variable isn't set there or in the real
    # environment, the default declared above is used instead.
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8")


# Created once, at import time, and reused everywhere via `from app.config import settings`.
# This is a common pattern: build the object once instead of every module
# re-reading and re-validating the .env file for itself.
settings = Settings()
