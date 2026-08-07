"""
Long-term memory: durable facts about the user.

Two storage layers, deliberately:

  SQLite (facts table)  - the source of truth. Readable, backup-able,
                          queryable with plain SQL.
  Chroma (vector index) - a derived search structure that answers "which
                          facts MEAN something similar to this question?".

Chroma can be deleted and rebuilt from the facts table at any time, which is
why chroma_data/ is gitignored without a second thought.

Why a vector index at all: SQL can find facts containing the word "audio".
It cannot find "prefers sounddevice over PyAudio" when asked "what do I use
for sound?" -- no shared words. An embedding turns text into a list of
numbers representing its MEANING, and similar meanings get similar numbers,
so the two land near each other even with no vocabulary in common. Chroma
stores those numbers and answers "find the N closest".

Chroma computes embeddings locally with a small bundled model (downloaded on
first use, ~80MB). No API, no cost, nothing leaves the machine.

ONE PROCESS AT A TIME. Chroma is embedded, not a server: a client holds the
index in memory and does not see writes made by another process against the
same directory. Facts written by a script while the server is running stay
invisible to it until the server restarts. Observed the hard way -- searches
returned nothing for facts that were provably on disk. If a tool ever needs
to write facts while JARVIS is running, it must go through the running
process (an API call), not open its own client.
"""

import logging

import chromadb
from chromadb.config import Settings as ChromaSettings
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Fact

logger = logging.getLogger(__name__)

COLLECTION_NAME = "facts"


class MemoryStore:
    """Stores and retrieves durable facts."""

    def __init__(self, chroma_client: chromadb.ClientAPI | None = None) -> None:
        """
        Args:
            chroma_client: injected by tests (an in-memory client, so tests
                never touch the real index on disk). Production passes
                nothing and gets the persistent on-disk client.
        """
        if chroma_client is None:
            chroma_client = chromadb.PersistentClient(
                path=settings.chroma_dir,
                # Chroma sends anonymous usage telemetry by default. Off:
                # this assistant's entire premise is that it runs locally.
                settings=ChromaSettings(anonymized_telemetry=False),
            )
        self._collection = self._open_collection(chroma_client)

    @staticmethod
    def _open_collection(client: chromadb.ClientAPI):
        """
        Open the facts collection, forcing cosine distance.

        Chroma defaults to squared-L2, whose numbers depend on vector
        magnitude and so have no stable meaning across texts. Cosine distance
        runs 0 (identical) to 2 (opposite) regardless of length, which is what
        makes a relevance cutoff possible at all -- see search().

        A collection created with the old metric is thrown away and recreated
        empty. That is safe: the index is derived data, and the startup hook
        in main.py rebuilds it from the facts table.
        """
        try:
            existing = client.get_collection(COLLECTION_NAME)
            if (existing.metadata or {}).get("hnsw:space") == "cosine":
                return existing
            logger.warning(
                "Memory index uses the wrong distance metric; recreating it. "
                "It will be rebuilt from the facts table on next startup."
            )
            client.delete_collection(COLLECTION_NAME)
        except Exception:
            pass  # no collection yet -- the normal first-run path

        return client.create_collection(
            COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
        )

    def remember(
        self,
        db: Session,
        user_id: int,
        content: str,
        kind: str = "other",
        source_conversation_id: int | None = None,
    ) -> Fact | None:
        """
        Store one fact. Returns None if it was already known.

        Deduplication here is an exact-text match, deliberately.

        WHY NOT SEMANTIC DEDUPLICATION. The obvious idea -- skip a fact whose
        nearest neighbour is closer than some distance threshold -- was tried
        and measured against real data. Cosine distances:

            reworded duplicates actually observed .... 0.229 - 0.290
            "prefers dark mode" vs "prefers LIGHT mode" .... 0.131
            guitar vs piano (related but genuinely new) .... 0.457

        Any threshold high enough to catch the real duplicates (>= 0.29)
        also merges facts that mean OPPOSITE things. The signal does not
        separate, so a threshold here would silently corrupt memory rather
        than tidy it.

        Duplicates are prevented upstream instead: the extractor is shown the
        facts we already hold and told not to restate them. See
        core/prompts.py -> build_extraction_prompt.
        """
        content = content.strip()
        if not content:
            return None

        already_known = db.scalars(
            select(Fact).where(Fact.user_id == user_id, Fact.content == content)
        ).first()
        if already_known is not None:
            return None

        fact = Fact(
            user_id=user_id,
            content=content,
            kind=kind,
            source_conversation_id=source_conversation_id,
        )
        db.add(fact)
        db.commit()

        # Index for semantic search. If this fails the fact is still safely in
        # SQLite -- it just won't be findable by meaning until the index is
        # rebuilt, so a failure here must not lose the write.
        try:
            self._collection.add(
                ids=[str(fact.id)],
                documents=[content],
                metadatas=[{"user_id": user_id, "kind": kind}],
            )
        except Exception:
            logger.error("Failed to index fact %s in Chroma", fact.id, exc_info=True)

        logger.info("Remembered (%s): %s", kind, content)
        return fact

    def search(self, user_id: int, query: str, limit: int | None = None) -> list[str]:
        """
        Return the facts relevant to `query`, best match first.

        RELEVANCE, NOT JUST RANK. Chroma returns the nearest N whether or not
        any of them are actually related, so with a small fact store every
        query used to return everything. That is worse than useless for the
        fact extractor: shown facts about guitars while reading about Python,
        llama3.2 stopped extracting entirely (measured -- 2 facts became 0).

        Cosine distances measured against the real fact store, using the
        kind of queries that actually arrive (questions, not statements --
        questions sit noticeably further away, which is why an earlier cutoff
        calibrated on statement-to-statement distances wrongly filtered out
        relevant facts):

            statement matching a stored fact ......... 0.34
            QUESTION matching a stored fact .......... 0.52 - 0.60
            ---------------- decision boundary ----------------
            question about something unrelated ....... 0.81 - 0.96

        The gap between 0.60 and 0.81 is wide and clean, so the cutoff sits
        at 0.7. It must also stay above the 0.23 - 0.29 range where reworded
        duplicates live, so potential duplicates still reach the extractor.

        Returns plain strings because that is all the caller needs -- the
        text goes straight into a prompt.
        """
        if limit is None:
            limit = settings.memory_search_limit

        try:
            result = self._collection.query(
                query_texts=[query],
                n_results=limit,
                # Scoped to this user. Single-user today, but filtering now
                # means multi-user later is data, not a rewrite.
                where={"user_id": user_id},
                include=["documents", "distances"],
            )
        except Exception:
            # Memory is an enhancement, never a hard dependency. A broken
            # index must degrade the reply, not break the conversation.
            logger.error("Memory search failed", exc_info=True)
            return []

        # Chroma returns one result list per query text; we only sent one.
        documents = (result.get("documents") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]

        return [
            document
            for document, distance in zip(documents, distances)
            if distance <= settings.memory_relevance_cutoff
        ]

    def count(self) -> int:
        """How many facts are currently in the vector index."""
        try:
            return self._collection.count()
        except Exception:
            logger.error("Could not count the memory index", exc_info=True)
            return 0

    def rebuild_index(self, db: Session) -> int:
        """
        Recreate the whole vector index from the facts table.

        The escape hatch that makes "Chroma is derived data" true in practice:
        if the index is corrupted or deleted, nothing is lost.
        """
        facts = list(db.scalars(select(Fact)).all())
        if not facts:
            return 0

        self._collection.upsert(
            ids=[str(f.id) for f in facts],
            documents=[f.content for f in facts],
            metadatas=[{"user_id": f.user_id, "kind": f.kind} for f in facts],
        )
        logger.info("Rebuilt memory index from %s facts", len(facts))
        return len(facts)
