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

    # Speaking rate. 1.0 is the voice's own default, which measures ~111
    # words per minute -- noticeably slower than natural conversational
    # English at 150-160. Higher is faster. Measured on en_GB-alan-medium:
    #   1.00 -> 111 wpm     1.25 -> 132 wpm
    #   1.15 -> 124 wpm     1.40 -> 144 wpm
    tts_speed: float = 1.25

    # --- File system tools -------------------------------------------------
    # The ONLY directory tree tools may touch. Defaults to your home folder,
    # which keeps C:\Windows and Program Files out of reach while leaving
    # documents and projects usable. Narrow it to a single workspace if you
    # want a tighter blast radius.
    fs_root: str = str(Path.home())

    # Cap on how much of a file read_file will return. Without it, a tool
    # could load a multi-gigabyte file into memory and then into a prompt.
    fs_max_read_bytes: int = 200_000

    # Cap on directory listing and search results, for the same reason.
    fs_max_entries: int = 200

    # How long a pending confirmation stays valid. A stale "yes" must not be
    # redeemable an hour later against a request the user has forgotten.
    tool_confirmation_ttl_seconds: int = 300

    # Whether the model may call tools at all. Turning this off makes JARVIS
    # a pure chatbot again -- useful for comparing behaviour, or for running
    # a small local model that calls tools badly.
    tools_enabled: bool = True

    # How many times the model may call tools within a single turn before
    # the loop gives up. Stops a confused model from looping forever, e.g.
    # reading the same file repeatedly because it isn't satisfied.
    agent_max_tool_steps: int = 5

    # --- Internet tools ----------------------------------------------------
    # How long to wait for a page. Short: a page that slow is not worth the
    # user staring at a stalled conversation.
    web_timeout_seconds: float = 20.0

    # Stop downloading after this much. Without a cap, "fetch this URL" could
    # point at a multi-gigabyte file and exhaust memory.
    web_max_download_bytes: int = 3_000_000

    # Cap on extracted text handed to the model. A long article can be tens of
    # thousands of words, and every one costs tokens.
    web_max_text_chars: int = 20_000

    # Default number of search results. More is rarely better -- the model
    # reads all of them, and the first few carry most of the signal.
    web_search_max_results: int = 5

    # Sent as User-Agent. Honest identification rather than pretending to be
    # a browser: sites that do not want bots deserve to be able to tell.
    web_user_agent: str = "JARVIS-personal-assistant/1.0"

    # --- Automation --------------------------------------------------------
    # How often the scheduler asks the database whether anything is due.
    # A reminder can therefore be up to this many seconds late, which is a
    # fair trade for a scheduler that holds no state and survives restarts.
    reminder_check_seconds: int = 20

    # Cap on how far ahead a reminder may be set. Guards against a model
    # miscalculating a date and quietly scheduling something for the year
    # 3000, which would sit in the table forever.
    reminder_max_days_ahead: int = 365

    # How often to ask whether a scheduled task is due. Separate from the
    # reminder check because the two cost wildly different amounts: a due
    # reminder is a string lookup, a due task is a full model call.
    task_check_seconds: int = 30

    # The shortest repeat allowed. This is a spending limit as much as a
    # sanity check -- every run is an LLM call, so "every 5 seconds" would
    # quietly burn money all night. Five minutes is the floor.
    task_min_interval_seconds: int = 300

    # How many active tasks one user may have. A model that misreads "every
    # morning" as "make me a task" should hit a wall, not fill the table.
    task_max_active: int = 20

    # --- File watchers -----------------------------------------------------
    # Whether to watch folders at all. Off switches the whole subsystem
    # without deleting anyone's watches.
    watchers_enabled: bool = True

    # Ignore repeat changes to the same file within this many seconds. Saving
    # a file in an editor produces several OS events -- a temp file, a rename,
    # a metadata touch -- and reporting each one is noise, not information.
    watch_debounce_seconds: float = 2.0

    # Hard cap on notifications per minute. A build, a git checkout or an
    # unzip can produce thousands of events in a second; past this the
    # watcher sends one summary line instead of flooding the socket.
    watch_max_events_per_minute: int = 60

    # How many folders may be watched at once.
    watch_max_folders: int = 10

    # How often the watcher re-reads the database so a newly added watch
    # starts working. The tools only write rows; nothing reaches into the
    # running observer, so the database stays the only state.
    watch_sync_seconds: int = 30

    # --- Plugins -----------------------------------------------------------
    # Whether to load plugins at all. A plugin is ordinary Python running in
    # this process with this process's permissions -- there is no sandbox --
    # so this switch exists to turn the whole mechanism off.
    plugins_enabled: bool = True

    # Folder that plugin .py files are read from. Deliberately outside
    # backend/app: a plugin is something you drop in, not something you add
    # to the source tree.
    plugins_dir: str = str(PROJECT_ROOT / "plugins")

    # --- Vision ------------------------------------------------------------
    # Which engine answers questions about images: anthropic | ollama
    vision_provider: str = "anthropic"

    # Model used when vision_provider=anthropic. Any current Claude model
    # can see; this is separate from llm_model so chat and vision can differ.
    vision_model: str = "claude-opus-5"

    # Model used when vision_provider=ollama. moondream is ~1.7GB and the
    # smallest usable local option; expect 30-60s per image on a CPU.
    ollama_vision_model: str = "moondream"

    # OCR engine. rapidocr runs on onnxruntime (already installed for the
    # memory index), so it needs no extra runtime and no external binary.
    ocr_provider: str = "rapidocr"

    # Longest edge, in pixels, that a captured image is scaled down to.
    # Vision models bill by image size and gain nothing from detail beyond
    # roughly this, but going lower starts to blur small text.
    vision_max_dimension: int = 1568

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
