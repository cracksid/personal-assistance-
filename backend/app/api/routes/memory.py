"""
REST endpoints for the memory viewer.

    GET    /memory/facts        -> everything JARVIS remembers about you
    DELETE /memory/facts/{id}   -> forget one
    POST   /memory/rebuild      -> rebuild the vector index from the table

WHY A DELETE BUTTON IS A FEATURE AND NOT A CONVENIENCE.

Facts are extracted automatically from conversation by a model, and models
get things wrong. Before this, a wrong fact -- "Sid uses PyAudio", say --
was permanent, invisible, and quietly shaped every later answer. The only
way to see the facts table was a SQL client, and the only way to fix it was
DELETE FROM facts.

Memory you cannot inspect is memory you cannot trust, and memory you cannot
correct is worse than none.

The listing is the SQLite table, not the Chroma index, because the table is
the source of truth. If the two disagree, the rebuild endpoint exists to
make the index match.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_memory_store
from app.db import crud
from app.db.models import Fact
from app.db.session import get_db
from app.memory.store import MemoryStore

router = APIRouter(prefix="/memory", tags=["memory"])
logger = logging.getLogger(__name__)


class FactInfo(BaseModel):
    id: int
    content: str
    kind: str
    created_at: str
    source_conversation_id: int | None


class FactListing(BaseModel):
    total: int
    indexed: int
    facts: list[FactInfo]


@router.get("/facts", response_model=FactListing)
def list_facts(
    db: Session = Depends(get_db),
    memory: MemoryStore = Depends(get_memory_store),
) -> FactListing:
    """Everything remembered, newest first."""
    owner = crud.get_or_create_owner(db)
    rows = db.scalars(
        select(Fact).where(Fact.user_id == owner.id).order_by(Fact.id.desc())
    ).all()

    return FactListing(
        total=len(rows),
        # Shown next to the total so a mismatch between the source of truth
        # and the derived index is visible rather than mysterious.
        indexed=memory.count(),
        facts=[
            FactInfo(
                id=f.id,
                content=f.content,
                kind=f.kind,
                created_at=f.created_at.isoformat(),
                source_conversation_id=f.source_conversation_id,
            )
            for f in rows
        ],
    )


class Forgotten(BaseModel):
    id: int
    content: str


@router.delete("/facts/{fact_id}", response_model=Forgotten)
def forget_fact(
    fact_id: int,
    db: Session = Depends(get_db),
    memory: MemoryStore = Depends(get_memory_store),
) -> Forgotten:
    """
    Delete one fact from both the table and the vector index.

    No confirmation gate here, deliberately. The gate governs what the MODEL
    may do without a human; this is the human, acting directly, on a single
    row they are looking at. Putting an "are you sure?" between someone and
    the delete button they just chose to press would be theatre.
    """
    owner = crud.get_or_create_owner(db)
    content = memory.forget(db, owner.id, fact_id)

    if content is None:
        raise HTTPException(status_code=404, detail=f"There is no fact #{fact_id}.")

    return Forgotten(id=fact_id, content=content)


class Rebuilt(BaseModel):
    indexed: int


@router.post("/rebuild", response_model=Rebuilt)
def rebuild_index(
    db: Session = Depends(get_db),
    memory: MemoryStore = Depends(get_memory_store),
) -> Rebuilt:
    """
    Recreate the vector index from the facts table.

    The escape hatch that makes "Chroma is derived data" true in practice.
    Also the fix if a delete removed the row but left the index entry --
    see MemoryStore.forget on why that failure direction was chosen.
    """
    count = memory.rebuild_index(db)
    return Rebuilt(indexed=count)
