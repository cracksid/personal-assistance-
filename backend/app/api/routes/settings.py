"""
REST endpoints for the settings page.

    GET   /settings          -> what can be changed, and its current value
    PATCH /settings          -> change one or more of them

Every rule about what is allowed lives in app/settings_store.py, not here.
This module validates the request shape and translates a refusal into a 400;
it does not decide what is safe.

WHAT THIS ENDPOINT CANNOT DO.

It cannot read or write the API key, or any other secret. Not because it
filters them out, but because the allow-list in settings_store never
contained them -- there is no code path from an HTTP request to a SecretStr
field. CLAUDE.md: secrets live in .env, are never committed, and are never
logged.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import settings_store
from app.db.session import get_db

router = APIRouter(prefix="/settings", tags=["settings"])
logger = logging.getLogger(__name__)


class SettingInfo(BaseModel):
    key: str
    label: str
    help: str
    kind: str
    choices: list[str]
    restart: bool
    value: str


class SettingsListing(BaseModel):
    settings: list[SettingInfo]


class SettingsPatch(BaseModel):
    # A mapping of key -> new value, both as text. Text rather than a union
    # because the real type is known server-side from the current value, and
    # a client should not have to know that "1.25" is a float and "1" is an
    # int.
    changes: dict[str, str] = Field(
        description="Setting name to new value, e.g. {'llm_provider': 'anthropic'}."
    )


@router.get("", response_model=SettingsListing)
def list_settings() -> SettingsListing:
    """Everything the UI is allowed to change, with its current value."""
    values = settings_store.current_values()
    return SettingsListing(
        settings=[
            SettingInfo(
                key=item.key,
                label=item.label,
                help=item.help,
                kind=item.kind,
                choices=list(item.choices),
                restart=item.restart,
                value=values[item.key],
            )
            for item in settings_store.EDITABLE
        ]
    )


@router.patch("", response_model=SettingsListing)
def update_settings(
    patch: SettingsPatch, db: Session = Depends(get_db)
) -> SettingsListing:
    """
    Change settings, then hand back the full list so the UI cannot drift.

    Applied one at a time and the first failure stops the rest. A partial
    apply is possible as a result -- and is better than the alternative,
    which is validating everything up front and then discovering the third
    assignment fails anyway.
    """
    for key, value in patch.changes.items():
        try:
            settings_store.set_override(db, key, value)
        except settings_store.NotEditable as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (ValueError, TypeError) as exc:
            # validate_assignment rejected it -- wrong type, or outside
            # whatever the field allows.
            raise HTTPException(
                status_code=400, detail=f"{value!r} is not valid for {key}: {exc}"
            ) from exc

    return list_settings()
