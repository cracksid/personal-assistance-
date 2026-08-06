"""
Application configuration.

Settings are declared once here as a typed class, then loaded from environment
variables and the .env file. Nothing else in the codebase should read
os.environ directly -- always go through the `settings` object this module
creates, so there's exactly one place that knows where config comes from.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # This nested class tells pydantic-settings *how* to load the values above:
    # read a file named ".env" in the project root, using UTF-8 text encoding.
    # If a variable isn't set in .env or the real environment, the default
    # value declared above is used instead.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


# Created once, at import time, and reused everywhere via `from app.config import settings`.
# This is a common pattern: build the object once instead of every module
# re-reading and re-validating the .env file for itself.
settings = Settings()
