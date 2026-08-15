"""
Deciding what is worth remembering.

This lives in core/ rather than memory/ on purpose. Judging which statements
are durable enough to keep is an assistant-level decision, and CLAUDE.md says
core/ is the only tier that knows it's an assistant. memory/ stays a dumb,
reliable storage layer with no idea an LLM exists.

KNOWN LIMITATION -- extraction quality tracks model size. Measured on
llama3.2 (3B): the identical exchange returned [] on one run and two correct
facts on the next two. The parsing is deterministic; the model is not. In
practice facts still accumulate, because extraction runs every turn and a
subject mentioned more than once gets more than one chance. Switching
LLM_PROVIDER to anthropic makes this markedly more reliable, at a cost per
turn.
"""

import json
import logging
import re

from pydantic import BaseModel, ValidationError

from app.core.prompts import EXTRACTION_PROMPT, build_extraction_prompt
from app.providers.base import ChatMessage, LLMProvider

logger = logging.getLogger(__name__)

VALID_KINDS = {"identity", "preference", "project", "other"}

# A ceiling on how much one exchange can add. A confused model asked to find
# facts will happily invent a dozen; this bounds the damage.
MAX_FACTS_PER_TURN = 8


class ExtractedFact(BaseModel):
    content: str
    kind: str = "other"


# A \uXXXX sequence that survived JSON decoding, meaning it arrived
# double-escaped.
_STRAY_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _decode_stray_escapes(text: str) -> str:
    """
    Fix escape sequences the model double-escaped.

    Found in the real fact store: "The user\\u2019s favourite tea is oolong."
    was stored with those eight characters literally in it, and had been
    going into every prompt since.

    json.loads already decodes \\u2019 into a right single quote correctly.
    Getting the literal back means the model emitted \\\\u2019 -- escaping
    its own escape -- so the parser faithfully produced exactly what was
    sent. The bug is upstream, in the model, and this is the repair.

    HONEST TRADEOFF: a fact genuinely *about* an escape sequence -- "the
    code uses \\u0041 for A" -- is mangled by this. That is judged the
    better failure. Facts are sentences about the user, escape sequences in
    them are almost always this bug, and the alternative is visible
    corruption in every prompt forever.
    """
    return _STRAY_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)


def parse_facts(raw: str) -> list[ExtractedFact]:
    """
    Pull a fact list out of whatever the model actually returned.

    Deliberately forgiving. Small local models wrap JSON in markdown fences,
    add "Here is the JSON:" preambles, or emit something unparseable. None of
    that should break a conversation -- the worst acceptable outcome is
    forgetting a fact, so every failure path returns an empty list.
    """
    if not raw or not raw.strip():
        return []

    # Take everything between the first [ and the last ] -- this survives
    # markdown fences and chatty preambles in one step.
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1 or end < start:
        logger.debug("No JSON array found in extraction output")
        return []

    try:
        items = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        logger.debug("Extraction output was not valid JSON")
        return []

    if not isinstance(items, list):
        return []

    facts: list[ExtractedFact] = []
    for item in items[:MAX_FACTS_PER_TURN]:
        if not isinstance(item, dict):
            continue
        try:
            fact = ExtractedFact(**item)
        except ValidationError:
            continue

        fact.content = _decode_stray_escapes(fact.content).strip()
        if not fact.content:
            continue
        # Clamp to a known category so an invented one can't leak into the
        # database and quietly break later filtering.
        if fact.kind not in VALID_KINDS:
            fact.kind = "other"
        facts.append(fact)

    return facts


async def extract_facts(
    provider: LLMProvider,
    user_text: str,
    assistant_text: str,
    known_facts: list[str] | None = None,
) -> list[ExtractedFact]:
    """
    Ask the model which parts of this exchange are worth keeping.

    This is a second model call per turn, so it runs after the reply has
    already been streamed to the user -- they never wait for it.

    `known_facts` are the facts we already store that relate to this
    exchange. Passing them in is what prevents the same fact being saved
    three times in three different phrasings.
    """
    prompt = build_extraction_prompt(user_text, assistant_text, known_facts)

    chunks: list[str] = []
    async for chunk in provider.stream_chat(
        [ChatMessage(role="user", content=prompt)], EXTRACTION_PROMPT
    ):
        chunks.append(chunk)

    return parse_facts("".join(chunks))
