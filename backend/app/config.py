"""
Application configuration.

Settings are declared once here as a typed class, then loaded from environment
variables and the .env file. Nothing else in the codebase should read
os.environ directly -- always go through the `settings` object this module
creates, so there's exactly one place that knows where config comes from.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# __file__ is this file's own path. .resolve() makes it absolute, then each
# .parent goes up one folder: config.py -> app/ -> backend/
# Using pathlib (not string concatenation) is a hard rule in CLAUDE.md --
# it gets Windows path separators right without any manual escaping.
BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BACKEND_DIR / "jarvis.db"


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

    # This nested class tells pydantic-settings *how* to load the values above:
    # read a file named ".env" in the project root, using UTF-8 text encoding.
    # If a variable isn't set in .env or the real environment, the default
    # value declared above is used instead.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


# Created once, at import time, and reused everywhere via `from app.config import settings`.
# This is a common pattern: build the object once instead of every module
# re-reading and re-validating the .env file for itself.
settings = Settings()
