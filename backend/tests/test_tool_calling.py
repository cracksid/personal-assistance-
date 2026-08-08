"""
Tests for the model calling tools (Phase 9b).

The question these answer is not "can a tool run" -- test_gate.py covers
that -- but "when the MODEL asks for a tool, does the right thing happen?"
In particular: does a destructive request still stop and wait for a human,
now that nobody typed the request by hand?

A scripted provider stands in for the model, so each test states exactly
what the model decided and the assertions are about our reaction to it.
"""

from collections.abc import AsyncIterator, Generator

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_agent, get_gate
from app.core.agent import Agent, tool_specs
from app.core.gate import ToolGate
from app.db import crud
from app.db.models import AuditLog
from app.db.session import get_db
from app.main import app
from app.memory.store import MemoryStore
from app.providers.base import (
    ChatMessage,
    LLMProvider,
    ToolCall,
    ToolSpec,
    TurnEvent,
)
from app.tools import registry
from app.tools.base import Tool, ToolContext, ToolResult


class Args(BaseModel):
    target: str = "x"


class Harmless(Tool):
    name = "harmless_thing"
    description = "Does something safe."
    input_schema = Args
    requires_confirmation = False

    def __init__(self) -> None:
        self.ran = 0

    def describe_action(self, args: Args) -> str:
        return f"Look at {args.target}"

    async def run(self, args: Args, context: ToolContext) -> ToolResult:
        self.ran += 1
        return ToolResult(output=f"saw {args.target}")


class Destructive(Harmless):
    name = "destructive_thing"
    description = "Destroys something."
    requires_confirmation = True

    def describe_action(self, args: Args) -> str:
        return f"PERMANENTLY DELETE {args.target}"


class ScriptedProvider(LLMProvider):
    """
    A model whose decisions are written in advance.

    Each entry in `script` is the list of events for one turn, so a test can
    say "first the model asks for a tool, then it replies with text" and
    assert on what happened in between.
    """

    supports_tools = True

    def __init__(self, script: list[list[TurnEvent]]) -> None:
        self.script = list(script)
        self.offered_tools: list[ToolSpec] | None = None
        self.turns_seen: list[list[ChatMessage]] = []

    async def stream_turn(
        self,
        messages: list[ChatMessage],
        system: str,
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[TurnEvent]:
        self.offered_tools = tools
        self.turns_seen.append(list(messages))
        events = self.script.pop(0) if self.script else [TurnEvent(type="end")]
        for event in events:
            yield event

    async def stream_chat(
        self, messages: list[ChatMessage], system: str
    ) -> AsyncIterator[str]:
        yield ""


class ToollessProvider(LLMProvider):
    """A provider from before tools existed -- it never overrode anything."""

    def __init__(self) -> None:
        self.received_system = ""

    async def stream_chat(
        self, messages: list[ChatMessage], system: str
    ) -> AsyncIterator[str]:
        self.received_system = system
        yield "plain reply"


@pytest.fixture
def tools() -> Generator[dict[str, Tool], None, None]:
    registry.reset()
    made = {"harmless": Harmless(), "destructive": Destructive()}
    for tool in made.values():
        registry.register(tool)
    yield made
    registry.reset()
    registry.load_builtin_tools()


@pytest.fixture
def conversation(db_session):
    owner = crud.get_or_create_owner(db_session)
    return owner, crud.create_conversation(db_session, owner)


def audit_rows(db) -> list[AuditLog]:
    return list(db.scalars(select(AuditLog).order_by(AuditLog.id)))


# --- offering tools to the model -------------------------------------------


def test_tool_specs_expose_a_json_schema_per_tool(tools):
    """
    The model is shown JSON Schema generated from each tool's Pydantic
    model. The tool author writes a Python class and never hand-writes a
    schema -- that is what keeps the two from drifting apart.
    """
    specs = {spec.name: spec for spec in tool_specs()}

    assert set(specs) == {"harmless_thing", "destructive_thing"}
    assert "target" in specs["harmless_thing"].input_schema["properties"]


@pytest.mark.anyio
async def test_no_tools_are_offered_without_a_gate(
    db_session, memory_store, conversation, tools
):
    """
    An Agent built without a gate has no way to run a tool safely, so it
    must not advertise any. Failing by having no tools is far better than
    failing by running them ungated.
    """
    owner, conv = conversation
    provider = ScriptedProvider([[TurnEvent(type="text", text="hi"), TurnEvent(type="end")]])
    agent = Agent(provider, memory_store)  # no gate

    async for _ in agent.respond(db_session, owner.id, conv.id, "hello"):
        pass

    assert provider.offered_tools is None


@pytest.mark.anyio
async def test_no_tools_are_offered_to_a_provider_that_cannot_use_them(
    db_session, memory_store, conversation, tools
):
    """
    A provider written before tools existed still works. Its inherited
    stream_turn ignores the tool list, so offering one would be a silent
    lie -- the agent checks supports_tools instead.
    """
    owner, conv = conversation
    provider = ToollessProvider()
    agent = Agent(provider, memory_store, ToolGate())

    events = [
        e async for e in agent.respond(db_session, owner.id, conv.id, "hello")
    ]

    assert [e.text for e in events] == ["plain reply"]


# --- the tool loop ---------------------------------------------------------


@pytest.mark.anyio
async def test_a_requested_tool_runs_and_its_result_goes_back_to_the_model(
    db_session, memory_store, conversation, tools
):
    """
    The core of Phase 9b: model asks, gate runs, result is fed back, model
    answers. Two round trips, and the second one can see the first's output.
    """
    owner, conv = conversation
    provider = ScriptedProvider(
        [
            # Turn 1: the model asks for a tool.
            [
                TurnEvent(
                    type="end",
                    tool_calls=[
                        ToolCall(id="c1", name="harmless_thing", arguments={"target": "notes"})
                    ],
                )
            ],
            # Turn 2: having seen the result, it answers.
            [TurnEvent(type="text", text="I saw notes."), TurnEvent(type="end")],
        ]
    )
    agent = Agent(provider, memory_store, ToolGate())

    events = [
        e async for e in agent.respond(db_session, owner.id, conv.id, "look at notes")
    ]

    assert tools["harmless"].ran == 1

    # The user sees the tool run, then the reply.
    assert [e.type for e in events] == ["tool", "text"]
    assert events[0].tool_name == "harmless_thing"
    assert events[0].ok is True

    # And the model's second turn included the tool's output.
    second_turn = provider.turns_seen[1]
    assert second_turn[-1].role == "tool"
    assert second_turn[-1].content == "saw notes"


@pytest.mark.anyio
async def test_a_destructive_tool_stops_the_turn_and_asks(
    db_session, memory_store, conversation, tools
):
    """
    THE test for this phase. The model asked to destroy something; the gate
    still refuses to act without a human, even though no human typed the
    request. The turn ends -- it does not park a coroutine waiting.
    """
    owner, conv = conversation
    provider = ScriptedProvider(
        [
            [
                TurnEvent(
                    type="end",
                    tool_calls=[
                        ToolCall(id="c1", name="destructive_thing", arguments={"target": "notes"})
                    ],
                )
            ],
            [TurnEvent(type="text", text="should never get here"), TurnEvent(type="end")],
        ]
    )
    agent = Agent(provider, memory_store, ToolGate())

    events = [
        e async for e in agent.respond(db_session, owner.id, conv.id, "delete notes")
    ]

    assert [e.type for e in events] == ["confirmation"]
    assert events[0].description == "PERMANENTLY DELETE notes"
    assert events[0].confirmation_id

    assert tools["destructive"].ran == 0  # nothing happened
    assert len(provider.turns_seen) == 1  # the model was not asked again


@pytest.mark.anyio
async def test_the_loop_stops_at_the_step_cap(
    db_session, memory_store, conversation, tools, monkeypatch
):
    """
    A confused model can ask for the same tool forever. The cap turns that
    into a bounded cost and an honest message rather than a hang.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "agent_max_tool_steps", 3)

    owner, conv = conversation
    forever = [
        TurnEvent(
            type="end",
            tool_calls=[ToolCall(id="c", name="harmless_thing", arguments={})],
        )
    ]
    provider = ScriptedProvider([forever, forever, forever, forever])
    agent = Agent(provider, memory_store, ToolGate())

    events = [
        e async for e in agent.respond(db_session, owner.id, conv.id, "go")
    ]

    assert tools["harmless"].ran == 3
    assert events[-1].type == "text"
    assert "stopped after several tool steps" in events[-1].text


# --- capability gaps are recorded ------------------------------------------


@pytest.mark.anyio
async def test_a_tool_the_model_wished_for_is_logged(
    db_session, memory_store, conversation, tools
):
    """
    Borrowed from another JARVIS project: log what the assistant could not
    do. When the model reaches for a capability that does not exist, that is
    a feature request produced by real use rather than by guesswork.
    """
    owner, conv = conversation
    provider = ScriptedProvider(
        [
            [
                TurnEvent(
                    type="end",
                    tool_calls=[ToolCall(id="c1", name="send_email", arguments={"to": "a@b.c"})],
                )
            ],
            [TurnEvent(type="text", text="I can't do that yet."), TurnEvent(type="end")],
        ]
    )
    agent = Agent(provider, memory_store, ToolGate())

    async for _ in agent.respond(db_session, owner.id, conv.id, "email someone"):
        pass

    missing = [r for r in audit_rows(db_session) if r.status == "unknown_tool"]
    assert [r.tool_name for r in missing] == ["send_email"]


# --- the new built-in tools ------------------------------------------------


@pytest.mark.anyio
async def test_remember_fact_stores_something_searchable(db_session, memory_store):
    """
    Explicit memory. Phase 6 extraction is a guess a model makes; this is
    the channel for "remember X" meaning it actually gets remembered.
    """
    from app.tools.builtin import RememberFact, RememberInput

    owner = crud.get_or_create_owner(db_session)
    context = ToolContext(db=db_session, user_id=owner.id, memory=memory_store)

    result = await RememberFact().run(
        RememberInput(content="The user's sister is called Priya", kind="identity"),
        context,
    )

    assert result.ok
    assert memory_store.search(owner.id, "what is the user's sister called?") == [
        "The user's sister is called Priya"
    ]


@pytest.mark.anyio
async def test_remember_fact_is_honest_about_already_knowing(db_session, memory_store):
    from app.tools.builtin import RememberFact, RememberInput

    owner = crud.get_or_create_owner(db_session)
    context = ToolContext(db=db_session, user_id=owner.id, memory=memory_store)
    args = RememberInput(content="The user plays guitar")

    await RememberFact().run(args, context)
    second = await RememberFact().run(args, context)

    # Storing a known fact is a success, not a failure -- the user's intent
    # ("make sure you know this") was satisfied either way.
    assert second.ok
    assert "Already knew" in second.output


@pytest.mark.anyio
async def test_current_time_reports_a_real_date(db_session):
    from datetime import datetime

    from app.tools.builtin import CurrentTime, NoArgs

    result = await CurrentTime().run(NoArgs(), ToolContext(db=db_session))

    assert result.ok
    assert str(datetime.now().year) in result.output


# --- end to end over the WebSocket -----------------------------------------


@pytest.fixture
def client(
    db_session: Session, memory_store: MemoryStore, tools
) -> Generator[tuple[TestClient, dict], None, None]:
    """A client whose model is scripted to ask for the destructive tool."""
    provider = ScriptedProvider(
        [
            [
                TurnEvent(
                    type="end",
                    tool_calls=[
                        ToolCall(id="c1", name="destructive_thing", arguments={"target": "notes"})
                    ],
                )
            ]
        ]
    )
    gate = ToolGate()

    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_agent] = lambda: Agent(provider, memory_store, gate)
    app.dependency_overrides[get_gate] = lambda: gate
    try:
        yield TestClient(app), tools
    finally:
        app.dependency_overrides.clear()


def test_websocket_asks_before_running_a_destructive_tool(client):
    test_client, tools = client

    with test_client.websocket_connect("/ws/chat") as ws:
        ws.send_text("delete my notes")

        frame = ws.receive_json()
        assert frame["type"] == "confirmation"
        assert "PERMANENTLY DELETE" in frame["description"]
        assert ws.receive_json()["type"] == "done"

        assert tools["destructive"].ran == 0  # still nothing


def test_websocket_confirm_actually_runs_it(client):
    test_client, tools = client

    with test_client.websocket_connect("/ws/chat") as ws:
        ws.send_text("delete my notes")
        confirmation = ws.receive_json()
        ws.receive_json()  # done

        ws.send_text(
            f'{{"type": "confirm", "confirmation_id": "{confirmation["confirmation_id"]}"}}'
        )

        result = ws.receive_json()
        assert result["type"] == "tool"
        assert result["ok"] is True
        assert tools["destructive"].ran == 1


def test_websocket_cancel_means_it_never_runs(client):
    test_client, tools = client

    with test_client.websocket_connect("/ws/chat") as ws:
        ws.send_text("delete my notes")
        confirmation = ws.receive_json()
        ws.receive_json()  # done

        ws.send_text(
            f'{{"type": "cancel", "confirmation_id": "{confirmation["confirmation_id"]}"}}'
        )

        result = ws.receive_json()
        assert result["ok"] is False
        assert tools["destructive"].ran == 0


def test_a_message_that_merely_looks_like_json_is_still_chat(client):
    """
    A user typing a JSON-ish message must not be mistaken for a command.
    Only objects with a recognised "type" are control frames.
    """
    from app.api.routes.chat import _parse_control

    assert _parse_control('{"type": "confirm", "confirmation_id": "x"}') is not None
    assert _parse_control('{"why does this fail?"}') is None
    assert _parse_control('{"type": "something else"}') is None
    assert _parse_control("what does {} mean in python?") is None
