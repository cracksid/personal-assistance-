"""
Settings that can be changed from the UI, layered over .env.

    .env  ->  Settings object  ->  overrides from the database

.env is the floor. This module reads the setting_overrides table on startup
and assigns each value onto the live Settings object, so a change made in
the UI survives a restart without anything ever rewriting .env.

THE RULE THAT MAKES THIS SAFE: AN ALLOW-LIST, NOT A DENY-LIST.

EDITABLE below names every setting the API will change. Anything absent is
refused, including settings that do not exist -- so a typo is an error
rather than a stray attribute quietly added to the object.

It is an allow-list because a deny-list gets this wrong exactly once and
then it is wrong forever: someone adds a setting in six months, forgets to
deny it, and it becomes editable over HTTP. With an allow-list, forgetting
means a new setting is NOT editable, which is the safe direction to fail.

SECRETS ARE NOT IN IT AND CANNOT BE.

anthropic_api_key is a SecretStr and is not listed, so it cannot be read or
written through the API. That is deliberate and is why this table exists at
all rather than a .env writer: CLAUDE.md says secrets live in .env, are
never committed, and are never logged. An endpoint that rewrote that file
would put the key one bug away from being mangled or echoed back.

WHAT TAKES EFFECT WHEN.

Most of these are read at the moment they are used, so a change applies to
the next message. llm_provider and llm_model are read by the factory each
time an Agent is built -- once per WebSocket connection -- so switching
model applies to the next connection, not mid-turn. `restart` below says
which ones genuinely need one.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import SettingOverride

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Editable:
    """One setting the UI may change, and how to present it."""

    key: str
    label: str
    help: str
    # "text" | "number" | "toggle" | "choice"
    kind: str
    choices: tuple[str, ...] = ()
    # True if the change only takes effect after a restart, so the UI can
    # say so rather than leaving the user wondering why nothing happened.
    restart: bool = False


EDITABLE: tuple[Editable, ...] = (
    Editable(
        key="llm_provider",
        label="Model provider",
        help=(
            "anthropic is Claude via the cloud API: fast, reliable at "
            "choosing tools, costs money. ollama runs locally: free and "
            "private, but noticeably weaker at tool use."
        ),
        kind="choice",
        choices=("anthropic", "ollama"),
    ),
    Editable(
        key="llm_model",
        label="Model",
        help="Used when the provider is anthropic, e.g. claude-sonnet-5.",
        kind="text",
    ),
    Editable(
        key="ollama_model",
        label="Local model",
        help="Used when the provider is ollama, e.g. llama3.2.",
        kind="text",
    ),
    Editable(
        key="llm_effort",
        label="Reasoning effort",
        help="How hard Claude thinks before answering. Higher is slower and pricier.",
        kind="choice",
        choices=("low", "medium", "high", "xhigh", "max"),
    ),
    Editable(
        key="tools_enabled",
        label="Tools",
        help=(
            "Off makes JARVIS a plain chatbot with no ability to touch "
            "files, the web, or the scheduler."
        ),
        kind="toggle",
    ),
    Editable(
        key="agent_max_tool_steps",
        label="Max tool steps per turn",
        help="Stops a confused model looping on tools forever.",
        kind="number",
    ),
    Editable(
        key="memory_search_limit",
        label="Facts recalled per turn",
        help="How many remembered facts are pulled into the prompt. Each costs tokens.",
        kind="number",
    ),
    Editable(
        key="memory_relevance_cutoff",
        label="Memory relevance cutoff",
        help=(
            "Maximum cosine distance for a fact to count as relevant. Lower "
            "is stricter. Measured against the real fact store: 0.78."
        ),
        kind="number",
    ),
    Editable(
        key="vision_provider",
        label="Vision provider",
        help="Which engine answers questions about images.",
        kind="choice",
        choices=("anthropic", "ollama"),
    ),
    Editable(
        key="tts_speed",
        label="Speaking rate",
        help="1.0 is the voice's own pace. 1.25 measured as noticeably snappier.",
        kind="number",
    ),
    Editable(
        key="watchers_enabled",
        label="File watchers",
        help="Report changes in watched folders. Takes effect on restart.",
        kind="toggle",
        restart=True,
    ),
    Editable(
        key="plugins_enabled",
        label="Plugins",
        help="Load .py files from the plugins folder. Takes effect on restart.",
        kind="toggle",
        restart=True,
    ),
)

BY_KEY: dict[str, Editable] = {item.key: item for item in EDITABLE}


class NotEditable(Exception):
    """Raised for a key that is not on the allow-list."""


def _coerce(key: str, raw: str):
    """
    Turn stored text back into the type the field expects.

    Only three shapes appear in EDITABLE, so this is three branches rather
    than a general parser. Settings has validate_assignment=True, so
    anything that slips through here still fails at the assignment rather
    than being stored wrong.
    """
    current = getattr(settings, key)

    if isinstance(current, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    return raw


def apply_overrides(db: Session) -> int:
    """
    Load saved overrides onto the live Settings object. Returns how many.

    Called once at startup, after .env has been read. A row naming a key
    that is no longer editable -- because a later version removed it -- is
    skipped rather than raising: a stale row must not stop JARVIS starting.
    """
    applied = 0
    for row in db.scalars(select(SettingOverride)).all():
        if row.key not in BY_KEY:
            logger.warning("Ignoring stored override for unknown setting %r", row.key)
            continue
        try:
            setattr(settings, row.key, _coerce(row.key, row.value))
            applied += 1
        except Exception:
            logger.error("Could not apply stored override %r", row.key, exc_info=True)

    if applied:
        logger.info("Applied %s saved setting override(s)", applied)
    return applied


def set_override(db: Session, key: str, raw_value: str) -> None:
    """
    Change a setting and remember it. Raises NotEditable for anything else.

    Assigns first and saves second, on purpose: validate_assignment means a
    bad value raises here, and nothing should be written for a change that
    did not take.
    """
    if key not in BY_KEY:
        raise NotEditable(f"{key!r} is not a setting that can be changed here.")

    setattr(settings, key, _coerce(key, raw_value))

    row = db.scalars(
        select(SettingOverride).where(SettingOverride.key == key)
    ).first()
    if row is None:
        db.add(SettingOverride(key=key, value=raw_value))
    else:
        row.value = raw_value
    db.commit()

    logger.info("Setting %s changed to %r", key, raw_value)


def current_values() -> dict[str, str]:
    """Every editable setting's current value, as text."""
    return {item.key: str(getattr(settings, item.key)) for item in EDITABLE}
