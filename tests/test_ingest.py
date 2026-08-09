"""/ingest + /session/end behaviour through the HTTP surface (FR-1.1).

The capture intake accepts Tier 1 host hook payloads (design/06 2.5),
segments them into structured Turns, and hands them to the CapturePipeline
seam. Identity is never guessed: the explicit profile_id scopes turns and
missing profile_id is a clear 4xx. All assertions read structured turns from
the seam (the in-memory pipeline injected via the app lifespan).
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi.testclient import TestClient

from mnemoseed.capture import InMemoryCapturePipeline, TurnSegmenter
from mnemoseed.daemon.app import create_app
from mnemoseed.schema.turn import HostId, TurnRole
from mnemoseed.storage.ports import TurnRange

SESSION = "sess-capture-1"
PROFILE = "prof-main"


def _user(text: str = "what is two plus two?", **extra) -> dict:
    return {
        "host": "claude_code",
        "event": "user_prompt",
        "session_id": SESSION,
        "profile_id": PROFILE,
        "ts": 1.0,
        "content": {"text": text},
        **extra,
    }


def _assistant(text: str = "its four", **extra) -> dict:
    return {
        "host": "claude_code",
        "event": "assistant_message",
        "session_id": SESSION,
        "profile_id": PROFILE,
        "ts": 2.0,
        "content": {"text": text},
        **extra,
    }


def _tool(name: str = "Bash", input_: dict | None = None, output: str = "8 passed", **extra) -> dict:
    return {
        "host": "claude_code",
        "event": "tool_use",
        "session_id": SESSION,
        "profile_id": PROFILE,
        "ts": 3.0,
        "content": {"tool_name": name, "input": input_ or {"cmd": "uv run pytest"}, "output": output},
        **extra,
    }


def _end(ts: float | None = None) -> dict:
    body = {"session_id": SESSION, "profile_id": PROFILE}
    if ts is not None:
        body["ts"] = ts
    return body


def _client(pipeline: InMemoryCapturePipeline) -> TestClient:
    app = create_app()

    @asynccontextmanager
    async def fake_lifespan(application):
        application.state.capture = pipeline
        application.state.segmenter = TurnSegmenter(pipeline)
        yield

    app.router.lifespan_context = fake_lifespan
    return TestClient(app)


def _settle(client: TestClient, body: dict | None = None) -> None:
    response = client.post("/session/end", json=body or _end())
    assert response.status_code == 200


def test_valid_user_message_produces_structured_turn() -> None:
    pipeline = InMemoryCapturePipeline()
    with _client(pipeline) as client:
        response = client.post("/ingest", json=_user())
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "accepted"
        assert body["session_id"] == SESSION
        assert body["profile_id"] == PROFILE
        assert body["event"] == "user_prompt"
        _settle(client)

    turns = pipeline.turns(SESSION)
    assert len(turns) == 1
    turn = turns[0]
    assert turn.turn_index == 0
    assert turn.profile_id == PROFILE
    assert turn.host == HostId.CLAUDE_CODE
    assert turn.closed is True
    assert [step.role for step in turn.steps] == [TurnRole.USER]
    assert turn.steps[0].content == "what is two plus two?"


def test_tool_call_sequence_preserved_in_order() -> None:
    pipeline = InMemoryCapturePipeline()
    with _client(pipeline) as client:
        assert client.post("/ingest", json=_user(text="run the suite")).status_code == 202
        assert client.post("/ingest", json=_tool(name="Bash", output="8 passed")).status_code == 202
        assert (
            client.post(
                "/ingest",
                json=_tool(name="Read", input_={"path": "a.py"}, output="<file>"),
            ).status_code
            == 202
        )
        assert client.post("/ingest", json=_assistant(text="all green")).status_code == 202
        _settle(client)

    turns = pipeline.turns(SESSION)
    assert len(turns) == 1
    steps = turns[0].steps
    assert [step.role for step in steps] == [TurnRole.USER, TurnRole.TOOL, TurnRole.TOOL, TurnRole.ASSISTANT]
    assert steps[1].tool_name == "Bash"
    assert steps[1].tool_input == {"cmd": "uv run pytest"}
    assert steps[1].content == "8 passed"
    assert steps[2].tool_name == "Read"
    assert steps[2].tool_input == {"path": "a.py"}
    assert steps[3].content == "all green"


def test_turn_boundaries_across_user_prompts() -> None:
    pipeline = InMemoryCapturePipeline()
    with _client(pipeline) as client:
        assert client.post("/ingest", json=_user(text="what is x?")).status_code == 202
        assert client.post("/ingest", json=_assistant(text="x is a var")).status_code == 202
        assert client.post("/ingest", json=_user(text="and y?")).status_code == 202
        assert client.post("/ingest", json=_assistant(text="y is a function")).status_code == 202
        _settle(client)

    turns = pipeline.turns(SESSION)
    assert [turn.turn_index for turn in turns] == [0, 1]
    assert turns[0].closed is True
    assert turns[1].closed is True
    assert [step.role for step in turns[0].steps] == [TurnRole.USER, TurnRole.ASSISTANT]
    assert [step.role for step in turns[1].steps] == [TurnRole.USER, TurnRole.ASSISTANT]
    assert turns[0].steps[0].content == "what is x?"
    assert turns[1].steps[0].content == "and y?"
    # the first turn ended when the second user prompt started
    assert turns[0].ended_at is not None


def test_implicit_turn_for_host_without_prompt_hook() -> None:
    # Cursor captures via afterAgentResponse + postToolUse only (design/06 2.5):
    # a response with no preceding user_prompt opens an implicit turn, and a
    # second response closes it and opens the next one.
    pipeline = InMemoryCapturePipeline()
    with _client(pipeline) as client:
        assert client.post("/ingest", json=_assistant(text="I will check", host="cursor")).status_code == 202
        assert client.post("/ingest", json=_tool(host="cursor")).status_code == 202
        assert (
            client.post("/ingest", json=_assistant(text="here is the result", host="cursor")).status_code
            == 202
        )
        _settle(client)

    turns = pipeline.turns(SESSION)
    assert len(turns) == 2
    assert turns[0].host == HostId.CURSOR
    assert [step.role for step in turns[0].steps] == [TurnRole.ASSISTANT, TurnRole.TOOL]
    assert [step.role for step in turns[1].steps] == [TurnRole.ASSISTANT]
    assert turns[0].steps[0].content == "I will check"
    assert turns[0].steps[1].tool_name == "Bash"


def test_missing_profile_id_rejected() -> None:
    pipeline = InMemoryCapturePipeline()
    with _client(pipeline) as client:
        payload = _user()
        del payload["profile_id"]
        response = client.post("/ingest", json=payload)
    assert response.status_code == 422
    assert "profile_id" in response.text


def test_missing_event_and_content_rejected() -> None:
    pipeline = InMemoryCapturePipeline()
    with _client(pipeline) as client:
        payload = _user()
        del payload["event"]
        assert client.post("/ingest", json=payload).status_code == 422
        payload = _user()
        del payload["content"]
        assert client.post("/ingest", json=payload).status_code == 422


def test_event_content_mismatch_rejected() -> None:
    pipeline = InMemoryCapturePipeline()
    with _client(pipeline) as client:
        tool_shaped = {"tool_name": "Bash", "input": {"cmd": "ls"}, "output": "a.py"}
        assert client.post("/ingest", json=_user(content=tool_shaped)).status_code == 422
        message_shaped = {"text": "not a tool call"}
        assert client.post("/ingest", json=_tool(content=message_shaped)).status_code == 422


def test_same_session_rejected_for_another_profile() -> None:
    pipeline = InMemoryCapturePipeline()
    with _client(pipeline) as client:
        assert client.post("/ingest", json=_user()).status_code == 202
        response = client.post("/ingest", json=_assistant(profile_id="prof-other"))
    assert response.status_code == 409
    assert "profile" in response.text


def test_profile_scopes_turns() -> None:
    pipeline = InMemoryCapturePipeline()
    other_session = "sess-capture-2"
    with _client(pipeline) as client:
        assert client.post("/ingest", json=_user(text="one")).status_code == 202
        assert (
            client.post(
                "/ingest",
                json=_user(session_id=other_session, profile_id="prof-work", text="two"),
            ).status_code
            == 202
        )
        _settle(client)
        assert (
            client.post(
                "/session/end",
                json={"session_id": other_session, "profile_id": "prof-work"},
            ).status_code
            == 200
        )

    turns_a = pipeline.turns(SESSION)
    turns_b = pipeline.turns(other_session)
    assert {turn.profile_id for turn in turns_a} == {PROFILE}
    assert {turn.profile_id for turn in turns_b} == {"prof-work"}
    assert turns_a[0].steps[0].content == "one"
    assert turns_b[0].steps[0].content == "two"


def test_session_end_closes_open_turn_range() -> None:
    pipeline = InMemoryCapturePipeline()
    with _client(pipeline) as client:
        assert client.post("/ingest", json=_user()).status_code == 202
        assert client.post("/ingest", json=_assistant()).status_code == 202
        response = client.post("/session/end", json=_end())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "settled"
    assert body["turns"] == 1
    assert body["turn_range"] == {"start": 0, "end": 0}
    assert pipeline.settled(SESSION) == TurnRange(start=0, end=0)
    # the open turn was flushed to the seam at settlement
    assert len(pipeline.turns(SESSION)) == 1


def test_session_end_is_idempotent() -> None:
    pipeline = InMemoryCapturePipeline()
    with _client(pipeline) as client:
        assert client.post("/ingest", json=_user()).status_code == 202
        first = client.post("/session/end", json=_end())
        second = client.post("/session/end", json=_end())
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["turn_range"] == {"start": 0, "end": 0}
    assert second.json()["turn_range"] == {"start": 0, "end": 0}
    assert len(pipeline.turns(SESSION)) == 1


def test_ingest_after_session_end_rejected() -> None:
    pipeline = InMemoryCapturePipeline()
    with _client(pipeline) as client:
        assert client.post("/ingest", json=_user()).status_code == 202
        assert client.post("/session/end", json=_end()).status_code == 200
        response = client.post("/ingest", json=_assistant(text="too late"))
    assert response.status_code == 409
    assert "settled" in response.text


def test_session_end_unknown_session_404() -> None:
    pipeline = InMemoryCapturePipeline()
    with _client(pipeline) as client:
        response = client.post(
            "/session/end",
            json={"session_id": "never-inexistent", "profile_id": PROFILE},
        )
    assert response.status_code == 404
    assert "not captured" in response.text


def test_session_end_missing_profile_id_rejected() -> None:
    pipeline = InMemoryCapturePipeline()
    with _client(pipeline) as client:
        response = client.post("/session/end", json={"session_id": SESSION})
    assert response.status_code == 422
    assert "profile_id" in response.text


def test_assistant_model_id_propagates_to_turn() -> None:
    # Regression pin (FR-1.6): the stamp needs model_id downstream, so a
    # silent drop in the segmenter must fail this test.
    pipeline = InMemoryCapturePipeline()
    with _client(pipeline) as client:
        assert client.post("/ingest", json=_user()).status_code == 202
        response = client.post(
            "/ingest",
            json=_assistant(content={"text": "four", "model_id": "claude-sonnet-5"}),
        )
        assert response.status_code == 202
        _settle(client)

    turn = pipeline.turns(SESSION)[0]
    assert turn.model_id == "claude-sonnet-5"


def test_session_end_settled_wrong_profile_is_409() -> None:
    # The profile check precedes the idempotent early-return: settling a
    # settled session under a different profile is a conflict, not a no-op.
    pipeline = InMemoryCapturePipeline()
    with _client(pipeline) as client:
        assert client.post("/ingest", json=_user()).status_code == 202
        assert client.post("/session/end", json=_end()).status_code == 200
        response = client.post(
            "/session/end",
            json={"session_id": SESSION, "profile_id": "prof-other"},
        )
    assert response.status_code == 409
    assert "profile" in response.text


def test_ingest_latency_far_under_caller_budget() -> None:
    # The caller side budgets 2s deadline with fail-open (design/06 4); this
    # stage owns only receive+parse+segment, which must stay near-zero. 60
    # in-process round trips completing under that budget is a design sanity
    # check, not a flaky wall-clock gate.
    pipeline = InMemoryCapturePipeline()
    started = time.perf_counter()
    with _client(pipeline) as client:
        for index in range(30):
            assert client.post("/ingest", json=_user(text=f"q{index}")).status_code == 202
            assert client.post("/ingest", json=_assistant(text=f"a{index}")).status_code == 202
        _settle(client)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"60 capture round trips took {elapsed:.3f}s (> 2s caller budget)"
    assert len(pipeline.turns(SESSION)) == 30
