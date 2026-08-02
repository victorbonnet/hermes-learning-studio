"""The runtime process: its control plane, its deadlines, and its handshake.

Run through Starlette's ``TestClient`` — real routing, real middleware — so the
control plane is exercised the way the supervisor reaches it rather than by
calling the handler functions directly. Nothing here opens a public socket,
starts a tunnel, or touches Telegram.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from learning_studio.runtime import environment as env
from learning_studio.runtime import server
from learning_studio.runtime.ownership import CONTROL_HEADER
from learning_studio.sessions import SessionStore

TOKEN = "control-token-for-tests"


def environ(**overrides) -> dict[str, str]:
    values = {
        env.RUNTIME_ID: "runtime-1",
        env.GENERATION: "3",
        env.CONTROL_TOKEN: TOKEN,
        env.PROFILE: "family",
        env.HANDSHAKE: "/tmp/handshake.json",
        env.IDLE_SECONDS: "60",
        env.MAX_LIFETIME_SECONDS: "300",
    }
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ── What the supervisor told it ───────────────────────────────────────────


def test_a_complete_environment_is_read():
    settings = server.settings_from_environment(environ())

    assert settings.runtime_id == "runtime-1"
    assert settings.generation == 3
    assert settings.profile == "family"
    assert settings.idle_timeout_seconds == 60
    assert settings.max_lifetime_seconds == 300


@pytest.mark.parametrize(
    "missing",
    [
        env.RUNTIME_ID,
        env.GENERATION,
        env.CONTROL_TOKEN,
        env.PROFILE,
        env.HANDSHAKE,
        env.IDLE_SECONDS,
        env.MAX_LIFETIME_SECONDS,
    ],
)
def test_a_missing_instruction_refuses_to_start(missing: str):
    """No defaults. Anything starting this process that is not the supervisor
    should find out immediately rather than get a working server."""
    with pytest.raises(server.BadEnvironment):
        server.settings_from_environment(environ(**{missing: None}))


@pytest.mark.parametrize(
    "override",
    [
        {env.GENERATION: "not-a-number"},
        {env.GENERATION: "0"},
        {env.IDLE_SECONDS: "-5"},
        {env.IDLE_SECONDS: "999999"},
        {env.MAX_LIFETIME_SECONDS: "0"},
        {env.HANDSHAKE: "relative/path.json"},
    ],
)
def test_a_malformed_instruction_refuses_to_start(override: dict):
    with pytest.raises(server.BadEnvironment):
        server.settings_from_environment(environ(**override))


def test_the_refusal_names_the_variable_and_never_its_value():
    """The reason is logged; one of these values is a credential."""
    with pytest.raises(server.BadEnvironment) as caught:
        server.settings_from_environment(environ(**{env.CONTROL_TOKEN: None}))

    assert env.CONTROL_TOKEN in str(caught.value)
    assert TOKEN not in str(caught.value)


# ── The control plane ─────────────────────────────────────────────────────


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def state(clock) -> server.RuntimeState:
    settings = server.settings_from_environment(environ())
    return server.RuntimeState(
        settings=settings,
        started_at=clock(),
        server_state="ready",
        port=45678,
        sessions=SessionStore(ttl_seconds=1800, max_sessions=10, clock=clock),
    )


@pytest.fixture
def client(state, clock):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    server.install_control_routes(app, state, clock=clock)
    # Starlette's default peer is the literal string "testclient", which is not
    # a loopback address and is correctly refused. A supervisor on the same
    # machine connects from one, so that is what these tests present.
    with TestClient(app, client=("127.0.0.1", 54321)) as test_client:
        yield test_client


def test_the_control_plane_answers_a_holder_of_the_secret(client, state):
    response = client.get("/internal/runtime", headers={CONTROL_HEADER: TOKEN})

    assert response.status_code == 200
    body = response.json()
    assert body["runtime_id"] == "runtime-1"
    assert body["generation"] == 3
    assert body["pid"] == os.getpid()
    assert body["server_state"] == "ready"


@pytest.mark.parametrize(
    "headers",
    [{}, {CONTROL_HEADER: ""}, {CONTROL_HEADER: "wrong"}, {CONTROL_HEADER: TOKEN + "x"}],
)
def test_the_control_plane_is_invisible_without_the_secret(client, headers: dict):
    """404, not 401: a wrong token and a route that does not exist look the same."""
    response = client.get("/internal/runtime", headers=headers)

    assert response.status_code == 404
    assert "runtime_id" not in response.text


def test_a_non_loopback_peer_is_refused_even_with_the_secret(state, clock):
    """The runtime is reachable through a public tunnel; the control plane is not."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    server.install_control_routes(app, state, clock=clock)

    with TestClient(app, client=("203.0.113.7", 1234)) as remote:
        response = remote.get("/internal/runtime", headers={CONTROL_HEADER: TOKEN})

    assert response.status_code == 404


def test_the_status_reply_carries_no_secret(client, state):
    body = client.get("/internal/runtime", headers={CONTROL_HEADER: TOKEN}).text

    assert TOKEN not in body
    for forbidden in ("control_token", "bot_token", "TELEGRAM"):
        assert forbidden not in body


def test_a_shutdown_request_is_recorded_once(client, state):
    state.stop_event = None

    assert client.post("/internal/shutdown", headers={CONTROL_HEADER: TOKEN}).status_code == 200
    assert state.server_state == "stopping"
    assert state.stop_reason == "control_request"

    client.post("/internal/shutdown", headers={CONTROL_HEADER: TOKEN})
    assert state.stop_reason == "control_request"


def test_a_shutdown_cannot_be_requested_without_the_secret(client, state):
    client.post("/internal/shutdown")

    assert state.server_state == "ready"


def test_grant_routes_refuse_when_the_runtime_serves_no_grants(client):
    """This commit ships the endpoints; the grant store arrives with launching."""
    for path in ("/internal/grant", "/internal/grant/revoke", "/internal/launch"):
        response = client.post(path, headers={CONTROL_HEADER: TOKEN}, json={})
        assert response.status_code == 409, path


def test_an_oversized_control_body_is_refused(client, state):
    state.grants = object()

    response = client.post(
        "/internal/grant",
        headers={CONTROL_HEADER: TOKEN},
        content=b'{"x":"' + b"y" * (server.MAX_CONTROL_BODY_BYTES + 10) + b'"}',
    )

    assert response.status_code == 400


# ── Idleness and deadlines ────────────────────────────────────────────────


def test_idleness_is_unknown_until_an_authenticated_learner_arrives(state, clock):
    assert state.idle_seconds(clock()) is None


def test_idleness_is_measured_from_authenticated_activity(state, clock):
    state.sessions.note_activity()
    clock.advance(30)

    assert state.idle_seconds(clock()) == 30


def run(coroutine):
    return asyncio.run(coroutine)


def test_the_watchdog_stops_an_idle_runtime(state, clock):
    state.stop_event = None

    async def scenario():
        state.stop_event = asyncio.Event()
        state.sessions.note_activity()
        clock.advance(state.settings.idle_timeout_seconds)
        await server._watchdog(state, clock=clock, tick=0.001)

    run(scenario())

    assert state.stop_reason == "idle_timeout"


def test_a_runtime_nobody_opened_still_stops(state, clock):
    """The idle clock starts at the runtime's own start, not at a first request."""

    async def scenario():
        state.stop_event = asyncio.Event()
        clock.advance(state.settings.idle_timeout_seconds)
        await server._watchdog(state, clock=clock, tick=0.001)

    run(scenario())

    assert state.stop_reason == "idle_timeout"


def test_the_watchdog_stops_a_busy_runtime_at_its_maximum_lifetime(state, clock):
    async def scenario():
        state.stop_event = asyncio.Event()
        clock.advance(state.settings.max_lifetime_seconds)
        state.sessions.note_activity()
        await server._watchdog(state, clock=clock, tick=0.001)

    run(scenario())

    assert state.stop_reason == "max_lifetime"


def test_the_watchdog_leaves_a_working_runtime_alone(state, clock):
    async def scenario():
        state.stop_event = asyncio.Event()
        state.sessions.note_activity()
        waiting = asyncio.create_task(server._watchdog(state, clock=clock, tick=0.001))
        await asyncio.sleep(0.05)
        state.stop_event.set()
        await waiting

    run(scenario())

    assert state.stop_reason == ""


def test_the_status_reports_how_long_is_left(state, clock):
    clock.advance(100)

    status = state.status(clock())

    assert status["expires_in_seconds"] == pytest.approx(200)


# ── The handshake ─────────────────────────────────────────────────────────


def test_the_handshake_is_written_atomically_and_owner_only(tmp_path: Path):
    path = tmp_path / "handshake.json"

    server.write_handshake(path, {"runtime_id": "r", "port": 4242})

    assert json.loads(path.read_text(encoding="utf-8"))["port"] == 4242
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


# ── What the runtime never does ───────────────────────────────────────────


def test_the_access_log_is_off_and_there_is_no_setting_that_turns_it_on():
    """An access log here is a file of one identifiable person's study session."""
    source = Path(server.__file__).read_text(encoding="utf-8")

    assert "access_log=False" in source
    assert "access_log=True" not in source


def test_an_unexpected_failure_is_reported_without_its_message(monkeypatch, caplog):
    """This process holds a bot token, a control secret, and a learner's answers."""

    valid = server.settings_from_environment(environ())

    def explode(*_args, **_kwargs):
        raise RuntimeError("bot token 123456:AAHsecret leaked into a traceback")

    monkeypatch.setattr(server.asyncio, "run", explode)
    monkeypatch.setattr(server, "settings_from_environment", lambda *_: valid)

    with caplog.at_level("ERROR"):
        code = server.main()

    assert code == server.EXIT_SERVE_FAILED
    assert "AAHsecret" not in caplog.text
    assert "123456" not in caplog.text
