"""
Tests for fact extraction parsing.

parse_facts is a pure function -- no model, no network, no database -- which
makes it cheap to test hard. That matters because it is the component most
exposed to unreliable input: whatever a small local model decided to emit.

The rule these tests encode: a confused model costs us a forgotten fact,
never a crash.
"""

from app.core.extraction import MAX_FACTS_PER_TURN, parse_facts


def test_parses_a_clean_json_array():
    raw = '[{"content": "The user is named Sid", "kind": "identity"}]'

    facts = parse_facts(raw)

    assert len(facts) == 1
    assert facts[0].content == "The user is named Sid"
    assert facts[0].kind == "identity"


def test_survives_markdown_fences_and_preamble():
    """Exactly what small local models actually return."""
    raw = 'Sure! Here is the JSON:\n```json\n[{"content": "The user likes tea"}]\n```\nHope that helps!'

    facts = parse_facts(raw)

    assert len(facts) == 1
    assert facts[0].content == "The user likes tea"
    assert facts[0].kind == "other"  # defaulted, since none was given


def test_empty_array_means_nothing_worth_remembering():
    assert parse_facts("[]") == []


def test_unknown_kind_is_clamped_to_other():
    """
    An invented category must not reach the database, or later filtering by
    kind silently misses rows.
    """
    raw = '[{"content": "The user likes tea", "kind": "beverage_preference"}]'

    assert parse_facts(raw)[0].kind == "other"


def test_too_many_facts_are_capped():
    raw = "[" + ",".join(f'{{"content": "fact {i}"}}' for i in range(50)) + "]"

    assert len(parse_facts(raw)) == MAX_FACTS_PER_TURN


def test_malformed_input_returns_nothing_rather_than_raising():
    for raw in [
        "",
        "   ",
        "I could not find any facts.",
        "[{broken json",
        '{"content": "not a list"}',
        "[null, 42, \"a string\"]",
        '[{"kind": "identity"}]',  # no content field
        '[{"content": "   "}]',  # blank content
    ]:
        assert parse_facts(raw) == [], f"should have returned [] for {raw!r}"


def test_known_facts_are_shown_to_the_extractor():
    """
    The deduplication fix. Without the already-known list the extractor
    re-derives facts it has already reported, in fresh wording each time --
    the observed failure was three rows all saying the user prefers
    sounddevice.
    """
    from app.core.prompts import build_extraction_prompt

    prompt = build_extraction_prompt(
        "I use sounddevice",
        "Noted.",
        known_facts=["The user prefers SoundDevice"],
    )

    assert "The user prefers SoundDevice" in prompt
    assert "Already stored" in prompt
    assert "Do NOT report any of these again" in prompt


def test_prompt_omits_the_known_section_when_nothing_is_known():
    from app.core.prompts import build_extraction_prompt

    prompt = build_extraction_prompt("hi", "hello", known_facts=[])

    assert "Already stored" not in prompt


def test_prompt_does_not_call_facts_stale_just_because_they_were_acknowledged():
    """
    Regression test for a real failure. An earlier prompt said "extract only
    NEW facts", and llama3.2 read "new" as "not already acknowledged in this
    conversation" -- so any fact the assistant replied to was dropped. It
    explained itself: "the assistant responded by acknowledging it, making no
    new durable fact extraction possible." Extraction went to 0 for 6 runs.

    The word "new" must therefore never appear unqualified, and the prompt
    must say outright that the assistant's reply is irrelevant.
    """
    from app.core.prompts import EXTRACTION_PROMPT, build_extraction_prompt

    assert "Ignore how the assistant" in EXTRACTION_PROMPT
    assert "already acknowledged" in EXTRACTION_PROMPT

    prompt = build_extraction_prompt("I use vim", "Noted.", known_facts=None)
    assert "only NEW" not in prompt
