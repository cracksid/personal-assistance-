"""
Tests for configuration loading.

These exist because of a real bug: env_file was originally the relative path
".env", which pydantic-settings resolves against the current working
directory. The server is started from backend/, so it looked for
backend/.env, found nothing, and silently fell back to every default -- the
app ran with settings nobody had chosen, and nothing reported a problem.

It stayed hidden for three phases because every value in .env happened to
equal its default, so the fallback was invisible. The first setting that
differed exposed it.
"""

from pathlib import Path

from app.config import ENV_FILE, PROJECT_ROOT, Settings


def test_env_file_path_is_absolute():
    """
    The regression test for the bug above. A relative path here works only
    when the process happens to start in the right directory, and fails
    silently when it doesn't.
    """
    configured = Settings.model_config["env_file"]

    assert Path(configured).is_absolute()


def test_env_file_points_at_the_project_root():
    assert ENV_FILE == PROJECT_ROOT / ".env"

    # Sanity-check that PROJECT_ROOT really is the repository root, rather
    # than some directory that merely happens to exist.
    assert (PROJECT_ROOT / ".gitignore").exists()
    assert (PROJECT_ROOT / "backend").is_dir()
