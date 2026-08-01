"""The protected Mini App API, end to end and against the network never.

Everything here runs through Starlette's ``TestClient``: real routing, real
middleware, real HMAC verification. Nothing is stubbed except the three pieces
of ambient state a test must control — the bot token, the profile allowlist,
and the clock — and none of those can switch authentication off. Every request
below carries an ``initData`` payload signed for real with a fake token.

No Telegram call is made, and no test in this file opens a socket.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from learning_studio import service
from learning_studio.config import LearningStudioConfig
from learning_studio.responses import INVALID_RESPONSE_MESSAGE
from learning_studio.sessions import SessionStore
from learning_studio.web.app import (
    INIT_DATA_HEADER,
    SESSION_HEADER,
    create_app,
)
from learning_studio.web.dependencies import Dependencies
from tests.component_examples import CANARY, all_canaries, example, manifest
from tests.init_data import BOT_TOKEN, OTHER_USER_ID, USER_ID, build_init_data
from tests.served_responses import response_for

NOW = 1_800_000_000


class Clock:
    def __init__(self, now: float = float(NOW)) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def config() -> LearningStudioConfig:
    return LearningStudioConfig()


@pytest.fixture
def allowlist() -> set[str]:
    """Mutable so a test can revoke access mid-session."""
    return {USER_ID}


@pytest.fixture
def deps(hermes_home, clock, config, allowlist) -> Dependencies:
    """Real service, real verification, controlled clock and allowlist."""
    return Dependencies(
        config=config,
        sessions=SessionStore(
            ttl_seconds=config.mini_app_session_ttl_seconds,
            max_sessions=config.mini_app_max_sessions,
            clock=clock,
        ),
        bot_token=lambda: BOT_TOKEN,
        allowed_users=lambda: frozenset(allowlist),
        profile=lambda: "default",
        clock=clock,
        load_experience=lambda principal, experience_id: service.delivery_bundle(
            principal=principal, experience_id=experience_id, config=config
        ),
        load_asset=lambda principal, asset_id: service.read_managed_asset(
            principal=principal, asset_id=asset_id, config=config
        ),
        component_aliases=lambda principal, experience_id, component_key: service.component_aliases(
            principal=principal,
            experience_id=experience_id,
            component_key=component_key,
            config=config,
        ),
    )


@pytest.fixture
def client(deps):
    from fastapi.testclient import TestClient

    with TestClient(create_app(deps)) as test_client:
        yield test_client


@pytest.fixture
def experience_id(hermes_home, principal, config) -> str:
    """A three-component exercise owned by the ``1001`` learner."""
    components = [
        example("multiple_choice", id="q-one"),
        example("short_answer", id="q-two"),
        example("true_false", id="q-three"),
    ]
    result = service.prepare_experience(
        principal=principal, manifest=manifest(components), config=config
    )
    return result["experience_id"]


def auth(user_id: str = USER_ID, age: int = 5, **kwargs) -> dict[str, str]:
    return {INIT_DATA_HEADER: build_init_data(user_id=user_id, auth_date=NOW - age, **kwargs)}


def open_session(client, experience_id: str, user_id: str = USER_ID):
    response = client.post(
        "/api/session", json={"experience_id": experience_id}, headers=auth(user_id)
    )
    assert response.status_code == 201, response.text
    return response.json()["session_token"], response.json()


def session_headers(token: str, user_id: str = USER_ID) -> dict[str, str]:
    return {**auth(user_id), SESSION_HEADER: token}


# ── Every route is protected ──────────────────────────────────────────────

PROTECTED_ROUTES = [
    ("GET", "/api/health"),
    ("POST", "/api/session"),
    ("GET", "/api/session/component"),
    ("POST", "/api/session/answer"),
    ("GET", "/api/session/result"),
    ("GET", "/api/assets/some-asset-id"),
]


@pytest.mark.parametrize(("method", "path"), PROTECTED_ROUTES)
def test_no_route_answers_without_telegram_auth(client, method: str, path: str):
    response = client.request(method, path, json={})

    assert response.status_code == 401
    assert response.json()["error"] == "Telegram authentication failed."


@pytest.mark.parametrize(("method", "path"), PROTECTED_ROUTES)
def test_no_route_answers_a_forged_payload(client, method: str, path: str):
    headers = {INIT_DATA_HEADER: build_init_data(auth_date=NOW, signed=False)}

    response = client.request(method, path, json={}, headers=headers)

    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path"), PROTECTED_ROUTES)
def test_no_route_answers_an_unauthorised_account(client, method: str, path: str):
    """Verified by Telegram, absent from the allowlist: still refused."""
    response = client.request(method, path, json={}, headers=auth(OTHER_USER_ID))

    assert response.status_code == 403


@pytest.mark.parametrize(("method", "path"), PROTECTED_ROUTES[2:])
def test_session_routes_refuse_a_missing_session(client, method: str, path: str):
    response = client.request(method, path, json={}, headers=auth())

    assert response.status_code == 401


def test_an_empty_allowlist_denies_everyone(client, allowlist):
    allowlist.clear()

    assert client.get("/api/health", headers=auth()).status_code == 403


def test_revoking_access_takes_effect_on_the_next_request(client, experience_id, allowlist):
    token, _ = open_session(client, experience_id)
    allowlist.clear()

    response = client.get("/api/session/component", headers=session_headers(token))

    assert response.status_code == 403


def test_a_group_launch_is_refused_at_the_api(client, experience_id):
    headers = {INIT_DATA_HEADER: build_init_data(auth_date=NOW, extra={"chat_type": "supergroup"})}

    response = client.post("/api/session", json={"experience_id": experience_id}, headers=headers)

    assert response.status_code == 401


def test_expired_init_data_cannot_open_a_session(client, experience_id):
    stale = auth(age=LearningStudioConfig().mini_app_init_data_max_age_seconds + 1)

    response = client.post("/api/session", json={"experience_id": experience_id}, headers=stale)

    assert response.status_code == 401


def test_a_missing_bot_token_refuses_rather_than_admits(deps, experience_id):
    from fastapi.testclient import TestClient

    unconfigured = Dependencies(
        config=deps.config,
        sessions=deps.sessions,
        bot_token=lambda: "",
        allowed_users=deps.allowed_users,
        profile=deps.profile,
        clock=deps.clock,
        load_experience=deps.load_experience,
        load_asset=deps.load_asset,
    )
    with TestClient(create_app(unconfigured)) as client:
        response = client.get("/api/health", headers=auth())

    assert response.status_code == 401


# ── The flow that must work ───────────────────────────────────────────────


def test_health_answers_an_authorised_account(client):
    response = client.get("/api/health", headers=auth())

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_a_full_exercise_can_be_served_and_answered(client, experience_id):
    token, opened = open_session(client, experience_id)

    assert opened["experience"]["component_count"] == 3
    assert opened["progress"] == {
        "position": 0,
        "component_count": 3,
        "answered": 0,
        "completed": False,
    }

    for index, component_id in enumerate(("q-one", "q-two", "q-three")):
        current = client.get("/api/session/component", headers=session_headers(token)).json()
        assert current["component"]["component_id"] == component_id
        assert current["progress"]["position"] == index

        answered = client.post(
            "/api/session/answer",
            json={
                "component_id": component_id,
                # Built from the component *as served*, aliased identifiers and
                # all — which is the only response a real client could produce.
                "response": response_for(
                    current["component"]["type"],
                    current["component"]["payload"].get("content", {}),
                ),
            },
            headers=session_headers(token),
        )
        assert answered.status_code == 200, answered.text
        assert answered.json()["recorded"] is True

    result = client.get("/api/session/result", headers=session_headers(token)).json()

    assert result["progress"]["completed"] is True
    assert result["progress"]["answered"] == 3
    assert sorted(result["answered_components"]) == ["q-one", "q-three", "q-two"]
    assert result["scored"] is False


def test_the_session_token_is_returned_once_and_is_opaque(client, experience_id):
    token, opened = open_session(client, experience_id)

    assert USER_ID not in token
    assert experience_id not in token
    assert "session_token" not in json.dumps(opened["experience"])


def test_answering_out_of_order_is_refused(client, experience_id):
    token, _ = open_session(client, experience_id)

    response = client.post(
        "/api/session/answer",
        json={"component_id": "q-three", "response": "skipping ahead"},
        headers=session_headers(token),
    )

    assert response.status_code == 409


def test_answering_after_completion_is_refused(client, experience_id):
    token, _ = open_session(client, experience_id)
    for component_id in ("q-one", "q-two", "q-three"):
        client.post(
            "/api/session/answer",
            json={"component_id": component_id, "response": "x"},
            headers=session_headers(token),
        )

    response = client.post(
        "/api/session/answer",
        json={"component_id": "q-three", "response": "again"},
        headers=session_headers(token),
    )

    assert response.status_code == 409


def test_a_session_expires(client, experience_id, clock, config):
    token, _ = open_session(client, experience_id)
    clock.advance(config.mini_app_session_ttl_seconds + 1)

    response = client.get("/api/session/component", headers=session_headers(token))

    assert response.status_code == 401


def test_a_session_opened_with_stale_init_data_still_lasts_its_advertised_life(
    client, experience_id, clock, config
):
    """The advertised TTL has to be the TTL a caller actually gets.

    Regression: the freshness bound for later calls was ``max(bootstrap window,
    session TTL)`` rather than their sum, so a session opened with ``initData``
    that was already 299 seconds old died after ~1501 seconds of the 1800 it
    had advertised — a 401 while the session store still considered it live.
    """
    almost_stale = config.mini_app_init_data_max_age_seconds - 1
    headers = auth(age=almost_stale)
    opened = client.post("/api/session", json={"experience_id": experience_id}, headers=headers)
    assert opened.status_code == 201
    token = opened.json()["session_token"]
    advertised = opened.json()["expires_in_seconds"]

    assert advertised == config.mini_app_session_ttl_seconds

    # One second before the advertised expiry, with the same launch payload.
    clock.advance(advertised - 1)
    still_live = client.get("/api/session/component", headers={**headers, SESSION_HEADER: token})

    assert still_live.status_code == 200

    # And it does expire on time — the window widened, the session did not.
    clock.advance(2)
    assert (
        client.get("/api/session/component", headers={**headers, SESSION_HEADER: token}).status_code
        == 401
    )


def test_a_session_cannot_be_continued_with_an_older_launch(client, experience_id, clock):
    """A newer payload may continue a session; an older captured one may not."""
    token, _ = open_session(client, experience_id)

    older = auth(age=200)  # signed before the payload that opened the session
    response = client.get("/api/session/component", headers={**older, SESSION_HEADER: token})

    assert response.status_code == 401


def test_a_newer_launch_continues_the_same_session(client, experience_id, clock):
    """Reopening the Mini App mid-exercise must not invalidate the session."""
    token, _ = open_session(client, experience_id)

    clock.advance(30)
    newer = auth(age=0)
    response = client.get("/api/session/component", headers={**newer, SESSION_HEADER: token})

    assert response.status_code == 200


# ── Cross-user, cross-session, cross-experience ───────────────────────────


def test_another_learners_experience_is_not_found(client, experience_id, other_principal, config):
    """The ``2002`` learner may not open ``1001``'s exercise, allowlist or not."""
    other = service.prepare_experience(
        principal=other_principal, manifest=manifest(), config=config
    )

    response = client.post(
        "/api/session", json={"experience_id": other["experience_id"]}, headers=auth(USER_ID)
    )

    assert response.status_code == 404
    assert response.json()["error"] == "No such prepared exercise for this learner."


def test_an_unknown_experience_and_someone_elses_are_the_same_answer(
    client, other_principal, config
):
    other = service.prepare_experience(
        principal=other_principal, manifest=manifest(), config=config
    )
    theirs = client.post(
        "/api/session", json={"experience_id": other["experience_id"]}, headers=auth()
    )
    nonexistent = client.post("/api/session", json={"experience_id": "0" * 32}, headers=auth())

    assert theirs.status_code == nonexistent.status_code == 404
    assert theirs.json() == nonexistent.json()


def test_a_session_token_is_useless_to_another_telegram_account(client, experience_id, allowlist):
    allowlist.add(OTHER_USER_ID)
    token, _ = open_session(client, experience_id)

    response = client.get(
        "/api/session/component", headers=session_headers(token, user_id=OTHER_USER_ID)
    )

    assert response.status_code == 401


def test_a_session_reads_only_its_own_experience(client, principal, config, experience_id):
    """Two experiences, one session: the session's own is the only one served."""
    second = service.prepare_experience(
        principal=principal,
        manifest=manifest([example("multiple_choice", id="other-question")]),
        config=config,
    )
    token, _ = open_session(client, experience_id)

    current = client.get("/api/session/component", headers=session_headers(token)).json()
    result = client.get("/api/session/result", headers=session_headers(token)).json()

    assert current["component"]["component_id"] == "q-one"
    assert result["experience_id"] == experience_id
    assert result["experience_id"] != second["experience_id"]


def test_a_forged_session_token_is_refused(client, experience_id):
    open_session(client, experience_id)

    response = client.get("/api/session/component", headers={**auth(), SESSION_HEADER: "x" * 43})

    assert response.status_code == 401


# ── Hidden data never leaves ──────────────────────────────────────────────


def test_no_response_ever_carries_an_answer_key(client, experience_id):
    token, opened = open_session(client, experience_id)
    bodies = [json.dumps(opened)]

    for component_id in ("q-one", "q-two", "q-three"):
        bodies.append(client.get("/api/session/component", headers=session_headers(token)).text)
        bodies.append(
            client.post(
                "/api/session/answer",
                json={"component_id": component_id, "response": "an answer"},
                headers=session_headers(token),
            ).text
        )
    bodies.append(client.get("/api/session/result", headers=session_headers(token)).text)

    everything = "\n".join(bodies)
    assert CANARY not in everything
    for marker in all_canaries():
        assert marker not in everything
    for hidden_key in ("answer", "evaluation", "rubric", "scoring", "hints", "branching"):
        assert f'"{hidden_key}"' not in everything


def test_the_bot_token_never_appears_in_a_response(client, experience_id):
    token, opened = open_session(client, experience_id)
    health = client.get("/api/health", headers=auth()).text

    assert BOT_TOKEN not in json.dumps(opened)
    assert BOT_TOKEN not in health
    assert BOT_TOKEN.split(":")[1] not in json.dumps(opened)


def test_no_response_echoes_the_init_data(client, experience_id):
    headers = auth()
    response = client.post("/api/session", json={"experience_id": experience_id}, headers=headers)

    assert headers[INIT_DATA_HEADER] not in response.text
    assert "auth_date" not in response.text


def test_a_whole_session_writes_no_secret_to_disk(client, hermes_home, experience_id):
    """The database and the profile directory must hold none of it.

    Not "no table has a column for it" — the bytes on disk are searched, so a
    future migration that started storing a session token, a bot token, or a
    raw payload fails here rather than in an incident.
    """
    headers = auth()
    opened = client.post(
        "/api/session", json={"experience_id": experience_id}, headers=headers
    ).json()
    token = opened["session_token"]
    client.post(
        "/api/session/answer",
        json={"component_id": "q-one", "response": "an answer"},
        headers=session_headers(token),
    )

    on_disk = b"".join(path.read_bytes() for path in hermes_home.rglob("*") if path.is_file())

    assert BOT_TOKEN.encode() not in on_disk
    assert token.encode() not in on_disk
    assert headers[INIT_DATA_HEADER].encode() not in on_disk
    assert b"test_learner" not in on_disk  # the Telegram display name Telegram sent
    assert b"WebAppData" not in on_disk


def test_the_learner_record_is_shared_with_the_chat_session(
    client, experience_id, principal, config
):
    """A Mini App session must resolve to the same learner as a chat session.

    The API derives its principal exactly as the Telegram gateway does —
    platform plus sender ID — so the exercise prepared in conversation is the
    one the Mini App opens. If this drifts, a learner silently acquires two
    records.
    """
    token, _ = open_session(client, experience_id)
    served = client.get("/api/session/result", headers=session_headers(token)).json()

    from_tools = service.get_experience(
        principal=principal, experience_id=experience_id, config=config
    )

    assert served["experience_id"] == from_tools["experience_id"]


# ── Request limits, rate limits, headers ──────────────────────────────────


def test_an_oversized_body_is_refused(client, experience_id, config):
    token, _ = open_session(client, experience_id)
    oversized = {"component_id": "q-one", "response": "x" * (config.mini_app_max_request_bytes + 1)}

    response = client.post("/api/session/answer", json=oversized, headers=session_headers(token))

    assert response.status_code == 413


def test_a_declared_oversize_body_is_refused_even_when_short(client, config):
    response = client.post(
        "/api/session",
        content=b"{}",
        headers={
            **auth(),
            "content-type": "application/json",
            "content-length": str(config.mini_app_max_request_bytes + 1),
        },
    )

    assert response.status_code == 413


def test_a_deeply_nested_answer_is_refused(client, experience_id):
    token, _ = open_session(client, experience_id)
    nested = {"a": {"b": {"c": {"d": {"e": {"f": "too deep"}}}}}}

    response = client.post(
        "/api/session/answer",
        json={"component_id": "q-one", "response": nested},
        headers=session_headers(token),
    )

    assert response.status_code == 400


def test_a_non_json_body_is_refused(client):
    response = client.post(
        "/api/session",
        content=b"not json at all",
        headers={**auth(), "content-type": "application/json"},
    )

    assert response.status_code == 400


def drive_asgi(app, *, method: str, path: str, headers: dict[str, str], chunks: list[bytes]):
    """Call the ASGI app directly, counting the body bytes it actually pulls.

    Instrumenting the ``receive`` channel is the only way to measure this
    honestly. Spying on ``Request.stream`` does not work: Starlette's
    ``BaseHTTPMiddleware`` *constructs* the stream generator for every request
    without consuming it, so a spy there fires even when the route reads
    nothing. And going through ``TestClient`` measures the transport's
    buffering rather than the application's.

    Returns ``(status, pulled)`` where ``pulled`` counts the messages and bytes
    the app asked for.
    """
    import asyncio

    remaining = list(chunks)
    pulled = {"messages": 0, "bytes": 0}
    messages: list[dict] = []

    async def receive():
        if not remaining:
            return {"type": "http.disconnect"}
        chunk = remaining.pop(0)
        pulled["messages"] += 1
        pulled["bytes"] += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": bool(remaining)}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 45678),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    return status, pulled


def test_a_declared_oversize_body_is_never_pulled_from_the_channel(deps, config):
    """The declaration must be refused before a single byte is read.

    Regression: the first version awaited ``request.body()`` and measured
    afterwards, so a caller declaring a gigabyte got a gigabyte buffered and
    *then* a 413.
    """
    limit = config.mini_app_max_request_bytes

    status, pulled = drive_asgi(
        create_app(deps),
        method="POST",
        path="/api/session",
        headers={
            **auth(),
            "content-type": "application/json",
            "content-length": str(limit * 64),
        },
        chunks=[b"x" * 1024] * 64,
    )

    assert status == 413
    assert pulled == {"messages": 0, "bytes": 0}


def test_an_undeclared_oversize_body_stops_being_pulled_at_the_limit(deps, config):
    """A chunked request declares nothing, so reading must abort mid-stream."""
    limit = config.mini_app_max_request_bytes
    chunk = b"x" * 1024
    offered = (limit // len(chunk)) * 8

    status, pulled = drive_asgi(
        create_app(deps),
        method="POST",
        path="/api/session",
        headers={**auth(), "content-type": "application/json"},
        chunks=[chunk] * offered,
    )

    assert status == 413
    # At most one chunk may cross the line — that is what "stop as soon as the
    # cumulative size is exceeded" means.
    assert pulled["bytes"] <= limit + len(chunk)
    assert pulled["messages"] < offered, "the whole oversized stream was read before refusing"


def test_a_body_within_the_limit_is_still_read_completely(deps, experience_id):
    """The guard must not truncate an ordinary request."""
    payload = json.dumps({"experience_id": experience_id}).encode()

    status, pulled = drive_asgi(
        create_app(deps),
        method="POST",
        path="/api/session",
        headers={
            **auth(),
            "content-type": "application/json",
            "content-length": str(len(payload)),
        },
        chunks=[payload[:5], payload[5:]],
    )

    assert status == 201
    assert pulled["bytes"] == len(payload)


def test_an_unexpected_failure_leaks_nothing_to_the_logs(deps, experience_id, caplog):
    """The 500 path must be as redacted as every other log line.

    Regression: the middleware called ``logger.exception``, which wrote the
    message and traceback — and therefore any path, SQL fragment, token, or
    answer key the failing code was holding — straight into the ordinary log.
    """

    from fastapi.testclient import TestClient

    def exploding(_principal, _experience_id):
        raise RuntimeError(
            "connect /Users/someone/.hermes/db: SELECT * FROM learners; "
            f"answer={CANARY}-leaked bot_token={BOT_TOKEN}"
        )

    broken = Dependencies(
        config=deps.config,
        sessions=deps.sessions,
        bot_token=deps.bot_token,
        allowed_users=deps.allowed_users,
        profile=deps.profile,
        clock=deps.clock,
        load_experience=exploding,
        load_asset=deps.load_asset,
    )
    with (
        caplog.at_level(logging.DEBUG),
        TestClient(create_app(broken), raise_server_exceptions=False) as broken_client,
    ):
        response = broken_client.post(
            "/api/session", json={"experience_id": experience_id}, headers=auth()
        )

    assert response.status_code == 500
    logs = caplog.text
    assert "/Users/someone/.hermes" not in logs
    assert "SELECT" not in logs
    assert CANARY not in logs
    assert BOT_TOKEN not in logs
    assert "Traceback" not in logs
    # The failure is still recorded — just as a class name, which a programmer
    # chose, rather than a message built from runtime values.
    assert '"event": "unhandled_error"' in logs
    assert "RuntimeError" in logs


def test_the_failure_is_still_recorded_usefully(deps, experience_id, caplog):
    """Redaction must not mean silence: what broke and where is still logged.

    An operator needs enough to know a 500 happened, on which route, and of
    what kind. Everything built from runtime values — the message, the
    traceback — is what stays out.
    """

    from fastapi.testclient import TestClient

    def exploding(_principal, _experience_id):
        raise KeyError("runtime detail that must not be logged")

    broken = Dependencies(
        config=deps.config,
        sessions=deps.sessions,
        bot_token=deps.bot_token,
        allowed_users=deps.allowed_users,
        profile=deps.profile,
        clock=deps.clock,
        load_experience=exploding,
        load_asset=deps.load_asset,
    )
    with (
        caplog.at_level(logging.DEBUG),
        TestClient(create_app(broken), raise_server_exceptions=False) as broken_client,
    ):
        broken_client.post("/api/session", json={"experience_id": experience_id}, headers=auth())

    logs = caplog.text
    assert '"event": "unhandled_error"' in logs
    assert '"route": "/api/session"' in logs
    assert '"reason": "KeyError"' in logs
    assert "runtime detail that must not be logged" not in logs


def test_no_second_logger_can_be_switched_on_to_leak_the_traceback():
    """There is no opt-in detail logger, because a switch gets flipped.

    A silenced-by-default logger was tried here and removed: pytest's log
    capture attaches handlers to loggers it did not create and reads records
    through ``propagate = False``, so "off by default" was not off. The
    property under test is that no such escape hatch exists.
    """
    import ast

    import learning_studio.web.security as security_module

    assert not hasattr(security_module, "diagnostics")

    # Parsed, not grepped: the module's docstring *describes* the design that
    # was removed, and a text search would read that description as a
    # violation. Only actual calls count.
    package = Path(security_module.__file__).parent
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute) and node.func.attr == "exception":
                offenders.append(f"{path.name}: .exception() call")
            if any(keyword.arg == "exc_info" for keyword in node.keywords):
                offenders.append(f"{path.name}: exc_info= argument")

    assert offenders == []


def test_requests_are_rate_limited_per_user(hermes_home, clock, allowlist):
    from fastapi.testclient import TestClient

    config = LearningStudioConfig(
        mini_app_rate_limit_requests=3, mini_app_rate_limit_window_seconds=60
    )
    deps = Dependencies(
        config=config,
        sessions=SessionStore(ttl_seconds=600, max_sessions=10, clock=clock),
        bot_token=lambda: BOT_TOKEN,
        allowed_users=lambda: frozenset(allowlist),
        profile=lambda: "default",
        clock=clock,
    )
    with TestClient(create_app(deps)) as client:
        statuses = [client.get("/api/health", headers=auth()).status_code for _ in range(5)]
        limited = client.get("/api/health", headers=auth())
        clock.advance(61)
        recovered = client.get("/api/health", headers=auth())

    assert statuses[:3] == [200, 200, 200]
    assert statuses[3:] == [429, 429]
    assert limited.headers["Retry-After"]
    assert recovered.status_code == 200


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("Cross-Origin-Opener-Policy", "same-origin"),
        ("Cross-Origin-Resource-Policy", "same-origin"),
    ],
)
def test_security_headers_are_present(client, header: str, expected: str):
    response = client.get("/api/health", headers=auth())

    assert response.headers[header] == expected


def test_the_content_security_policy_is_restrictive(client):
    policy = client.get("/api/health", headers=auth()).headers["Content-Security-Policy"]

    assert "default-src 'none'" in policy
    assert "script-src 'none'" in policy
    assert "frame-ancestors https://web.telegram.org" in policy
    assert "base-uri 'none'" in policy


def test_responses_are_never_cached(client):
    response = client.get("/api/health", headers=auth())

    assert "no-store" in response.headers["Cache-Control"]


def test_security_headers_are_present_on_failures_too(client):
    response = client.get("/api/health")

    assert response.status_code == 401
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]


def test_no_cross_origin_access_is_granted(client):
    response = client.get("/api/health", headers={**auth(), "Origin": "https://attacker.invalid"})

    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}


def test_the_api_publishes_no_schema_or_docs(client):
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert client.get(path).status_code == 404


def test_an_unexpected_failure_returns_a_safe_error(deps, experience_id):
    """A bug must not answer with a traceback, a path, or a SQL fragment."""
    from fastapi.testclient import TestClient

    def exploding(_principal, _experience_id):
        raise RuntimeError("connection to /Users/someone/.hermes/db failed: SELECT * FROM learners")

    broken = Dependencies(
        config=deps.config,
        sessions=deps.sessions,
        bot_token=deps.bot_token,
        allowed_users=deps.allowed_users,
        profile=deps.profile,
        clock=deps.clock,
        load_experience=exploding,
        load_asset=deps.load_asset,
    )
    with TestClient(create_app(broken), raise_server_exceptions=False) as client:
        response = client.post(
            "/api/session", json={"experience_id": experience_id}, headers=auth()
        )

    assert response.status_code == 500
    assert "SELECT" not in response.text
    assert ".hermes" not in response.text
    assert "Traceback" not in response.text
    assert response.headers["X-Content-Type-Options"] == "nosniff"


# ── Managed assets ────────────────────────────────────────────────────────


def _image_experience(hermes_home: Path, principal, config) -> tuple[str, str]:
    """An experience whose only component shows one imported managed image."""
    from PIL import Image

    source = hermes_home / "cache" / "images" / "diagram.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 3), (21, 84, 126)).save(source, format="PNG")

    alt_text = "A labelled diagram showing the major parts of a cell."
    imported = service.import_asset(
        principal=principal,
        source_path=str(source),
        title="Cell structure diagram",
        alt_text=alt_text,
        provenance="host_image_generation",
        config=config,
    )
    component = example("image_observation", id="q-image")
    component["content"]["image"] = {"asset_ref": imported["asset_id"], "alt_text": alt_text}
    prepared = service.prepare_experience(
        principal=principal, manifest=manifest([component]), config=config
    )
    return prepared["experience_id"], imported["asset_id"]


def test_a_referenced_asset_is_served_to_its_owner(client, hermes_home, principal, config):
    experience_id, asset_id = _image_experience(hermes_home, principal, config)
    token, _ = open_session(client, experience_id)

    response = client.get(f"/api/assets/{asset_id}", headers=session_headers(token))

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_an_asset_needs_a_session(client, hermes_home, principal, config):
    _experience_id, asset_id = _image_experience(hermes_home, principal, config)

    response = client.get(f"/api/assets/{asset_id}", headers=auth())

    assert response.status_code == 401


def test_an_asset_the_experience_does_not_reference_is_refused(
    client, hermes_home, principal, config, experience_id
):
    """Owning an image is not enough; the open exercise must actually use it."""
    _other_experience, asset_id = _image_experience(hermes_home, principal, config)
    token, _ = open_session(client, experience_id)

    response = client.get(f"/api/assets/{asset_id}", headers=session_headers(token))

    assert response.status_code == 404


def test_another_learners_asset_is_not_served(
    client, hermes_home, principal, other_principal, config
):
    experience_id, _asset_id = _image_experience(hermes_home, principal, config)
    token, _ = open_session(client, experience_id)
    _their_experience, their_asset = _image_experience(hermes_home, other_principal, config)

    response = client.get(f"/api/assets/{their_asset}", headers=session_headers(token))

    assert response.status_code == 404


@pytest.mark.parametrize("asset_id", ["../../etc/passwd", "..%2f..%2fsecret", "unknown-asset"])
def test_an_unusable_asset_identifier_is_refused(client, experience_id, asset_id: str):
    token, _ = open_session(client, experience_id)

    response = client.get(f"/api/assets/{asset_id}", headers=session_headers(token))

    assert response.status_code in (404, 400)
    assert b"root:" not in response.content


def test_a_tampered_asset_is_not_served(client, hermes_home, principal, config):
    """Integrity is re-checked on delivery, not trusted from import time."""
    experience_id, asset_id = _image_experience(hermes_home, principal, config)
    token, _ = open_session(client, experience_id)
    stored = next((hermes_home / "workspace" / "learning-studio" / "assets").glob("*"))
    stored.chmod(0o600)
    stored.write_bytes(b"swapped after import")

    response = client.get(f"/api/assets/{asset_id}", headers=session_headers(token))

    assert response.status_code == 404


# ── The response contract, at the route ───────────────────────────────────


def test_a_missing_response_no_longer_advances_the_exercise(client, experience_id):
    """The reported defect, exactly as reported.

    `{"component_id": "q-one"}` carries no answer at all, and used to be recorded
    as one — because the only check was that the body was bounded JSON.
    """
    token, _ = open_session(client, experience_id)

    response = client.post(
        "/api/session/answer",
        json={"component_id": "q-one"},
        headers=session_headers(token),
    )

    assert response.status_code == 400
    still_here = client.get("/api/session/component", headers=session_headers(token)).json()
    assert still_here["component"]["component_id"] == "q-one"
    assert still_here["progress"] == {
        "position": 0,
        "component_count": 3,
        "answered": 0,
        "completed": False,
    }


@pytest.mark.parametrize(
    "response_value",
    [None, "an answer", 42, [], {"value": "true"}, {"wrong_field": True}, {}],
)
def test_a_response_that_does_not_fit_its_component_is_refused(
    client, experience_id, response_value
):
    token, _ = open_session(client, experience_id)

    refused = client.post(
        "/api/session/answer",
        json={"component_id": "q-one", "response": response_value},
        headers=session_headers(token),
    )

    assert refused.status_code == 400
    assert (
        client.get("/api/session/component", headers=session_headers(token)).json()["progress"][
            "answered"
        ]
        == 0
    )


def test_a_refused_response_is_not_recorded_anywhere(client, experience_id):
    """State moves only after the contract is satisfied."""
    token, _ = open_session(client, experience_id)

    client.post(
        "/api/session/answer",
        json={"component_id": "q-one", "response": {"option_id": "invented"}},
        headers=session_headers(token),
    )
    result = client.get("/api/session/result", headers=session_headers(token)).json()

    assert result["answered_components"] == []
    assert result["progress"]["answered"] == 0


def test_the_refusal_says_nothing_about_what_was_submitted(client, experience_id):
    token, _ = open_session(client, experience_id)

    refused = client.post(
        "/api/session/answer",
        json={"component_id": "q-one", "response": {"option_id": "a-distinctive-guess"}},
        headers=session_headers(token),
    )

    assert "a-distinctive-guess" not in refused.text


def test_a_response_naming_a_canonical_identifier_is_refused(client, experience_id, config):
    """A client may name only the identifiers it was served.

    The canonical option ids never reach a learner, so one arriving in a request
    came from somewhere else — and it is refused for the same reason an invented
    one is.
    """
    from tests.component_examples import example

    canonical = example("multiple_choice")["content"]["options"][0]["id"]
    token, _ = open_session(client, experience_id)

    refused = client.post(
        "/api/session/answer",
        json={"component_id": "q-one", "response": {"option_id": canonical}},
        headers=session_headers(token),
    )

    assert refused.status_code == 400


def test_a_served_alias_is_accepted_and_stored_canonically(client, experience_id, deps):
    """The round trip: alias out, canonical in."""
    from tests.component_examples import example

    token, _ = open_session(client, experience_id)
    current = client.get("/api/session/component", headers=session_headers(token)).json()
    served = current["component"]["payload"]["content"]["options"]
    canonical_ids = {entry["id"] for entry in example("multiple_choice")["content"]["options"]}

    assert not {entry["id"] for entry in served} & canonical_ids

    accepted = client.post(
        "/api/session/answer",
        json={"component_id": "q-one", "response": {"option_id": served[0]["id"]}},
        headers=session_headers(token),
    )

    assert accepted.status_code == 200
    stored = next(iter(deps.sessions._sessions.values())).answers["q-one"]
    assert stored["option_id"] in canonical_ids


def test_the_alias_mapping_is_never_served_to_a_client(client, experience_id):
    token, opened = open_session(client, experience_id)
    bodies = [
        json.dumps(opened),
        client.get("/api/session/component", headers=session_headers(token)).text,
        client.get("/api/session/result", headers=session_headers(token)).text,
    ]

    for body in bodies:
        assert "aliases" not in body


def test_a_card_the_client_cannot_draw_may_still_be_skipped(client, experience_id):
    token, _ = open_session(client, experience_id)

    skipped = client.post(
        "/api/session/answer",
        json={"component_id": "q-one", "response": {"skipped": True}},
        headers=session_headers(token),
    )

    assert skipped.status_code == 200


# ── Alias resolution, through the real route ──────────────────────────────
#
# Every rejection below is checked for *state neutrality*: a refused submission
# must leave the position, the answered count, the stored answers, the completion
# flag and the reveal state exactly as they were. A contract that refuses the
# response but advances the exercise is not a contract.


def session_state(deps):
    """Everything a refused submission must not change."""
    session = next(iter(deps.sessions._sessions.values()))
    return {
        "position": session.position,
        "answered": len(session.answers),
        "answers": dict(session.answers),
        "completed": session.completed,
        "completed_at": session.completed_at,
        "revealed": dict(session.revealed),
    }


def assert_refused_and_unchanged(client, deps, token, response_value, before):
    """Submit, expect the generic refusal, and prove nothing moved."""
    refused = client.post(
        "/api/session/answer",
        json={"component_id": "q-one", "response": response_value},
        headers=session_headers(token),
    )

    assert refused.status_code == 400
    body = refused.json()
    assert body["error"] == INVALID_RESPONSE_MESSAGE
    # The message says nothing about which state failed, or about any identifier.
    assert "alias" not in refused.text.lower()
    assert session_state(deps) == before
    return refused


def alias_state(state, mapping=None):
    from learning_studio.service import ComponentAliases

    return lambda *_: ComponentAliases(state, dict(mapping or {}))


def served_option(client, token) -> str:
    current = client.get("/api/session/component", headers=session_headers(token)).json()
    return current["component"]["payload"]["content"]["options"][0]["id"]


def test_a_current_scheme_with_a_complete_mapping_resolves(client, deps, experience_id):
    token, _ = open_session(client, experience_id)
    served = served_option(client, token)
    canonical = {entry["id"] for entry in example("multiple_choice")["content"]["options"]}

    accepted = client.post(
        "/api/session/answer",
        json={"component_id": "q-one", "response": {"option_id": served}},
        headers=session_headers(token),
    )

    assert accepted.status_code == 200
    stored = next(iter(deps.sessions._sessions.values())).answers["q-one"]
    assert stored["option_id"] in canonical


def test_a_previous_head_record_still_resolves_through_the_route(
    client, deps, hermes_home, principal, config
):
    """The upgrade path: `aliases` present, `alias_scheme` absent.

    This is what the previous release wrote. The learner payload it serves is
    aliased, so the alias must be translated — not passed through, which is the
    defect, and not refused, which would strand every experience prepared before
    the upgrade.
    """
    from learning_studio import storage

    experience_id = service.prepare_experience(
        principal=principal,
        manifest=manifest([example("multiple_choice", id="q-one")]),
        config=config,
    )["experience_id"]

    with storage.connect(config) as conn:
        row = conn.execute(
            "SELECT e.component_id, e.evaluation"
            "  FROM experience_components AS c"
            "  JOIN experience_component_evaluations AS e ON e.component_id = c.id"
            " WHERE c.experience_id = ? AND c.component_key = ?",
            (experience_id, "q-one"),
        ).fetchone()
        stored = json.loads(row["evaluation"])
        # Exactly what c71466f wrote: a mapping, no scheme number, and no
        # canonical inventory — that field did not exist yet either.
        del stored["alias_scheme"]
        del stored["canonical_identifiers"]
        conn.execute(
            "UPDATE experience_component_evaluations SET evaluation = ? WHERE component_id = ?",
            (json.dumps(stored), row["component_id"]),
        )

    token, _ = open_session(client, experience_id)
    served = served_option(client, token)
    canonical = {entry["id"] for entry in example("multiple_choice")["content"]["options"]}
    assert served not in canonical, "the payload under test is not aliased"

    accepted = client.post(
        "/api/session/answer",
        json={"component_id": "q-one", "response": {"option_id": served}},
        headers=session_headers(token),
    )

    assert accepted.status_code == 200
    answered = next(iter(deps.sessions._sessions.values())).answers["q-one"]
    assert answered["option_id"] in canonical, "an alias was stored as if it were canonical"


@pytest.mark.parametrize(
    "state_name",
    ["aliased-empty-mapping", "aliased-incomplete-mapping", "unresolved"],
)
def test_an_unresolvable_identifier_is_refused_and_changes_nothing(
    client, deps, experience_id, state_name
):
    from learning_studio.service import AliasState

    token, _ = open_session(client, experience_id)
    served = served_option(client, token)
    real = deps.component_aliases(deps.principal(USER_ID), experience_id, "q-one")

    states = {
        "aliased-empty-mapping": alias_state(AliasState.ALIASED, {}),
        "aliased-incomplete-mapping": alias_state(
            AliasState.ALIASED,
            {a: c for a, c in real.mapping.items() if a != served},
        ),
        "unresolved": alias_state(AliasState.UNRESOLVED),
    }
    object.__setattr__(deps, "component_aliases", states[state_name])
    before = session_state(deps)

    refused = assert_refused_and_unchanged(client, deps, token, {"option_id": served}, before)
    assert served not in refused.text


def test_a_missing_evaluator_row_fails_closed(client, deps, hermes_home, principal, config):
    """Deleting the row is not evidence that the payload is canonical."""
    from learning_studio import storage

    experience_id = service.prepare_experience(
        principal=principal,
        manifest=manifest([example("multiple_choice", id="q-one")]),
        config=config,
    )["experience_id"]
    token, _ = open_session(client, experience_id)
    served = served_option(client, token)

    with storage.connect(config) as conn:
        conn.execute("DELETE FROM experience_component_evaluations")

    before = session_state(deps)
    assert_refused_and_unchanged(client, deps, token, {"option_id": served}, before)


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param({"alias_scheme": "1"}, id="malformed-scheme"),
        pytest.param({"alias_scheme": 99}, id="unsupported-future-scheme"),
        pytest.param({"aliases": "not-a-mapping"}, id="malformed-aliases"),
    ],
)
def test_a_damaged_alias_record_fails_closed(client, deps, hermes_home, principal, config, damage):
    from learning_studio import storage

    experience_id = service.prepare_experience(
        principal=principal,
        manifest=manifest([example("multiple_choice", id="q-one")]),
        config=config,
    )["experience_id"]
    token, _ = open_session(client, experience_id)
    served = served_option(client, token)

    with storage.connect(config) as conn:
        row = conn.execute(
            "SELECT component_id, evaluation FROM experience_component_evaluations"
        ).fetchone()
        stored = {**json.loads(row["evaluation"]), **damage}
        conn.execute(
            "UPDATE experience_component_evaluations SET evaluation = ? WHERE component_id = ?",
            (json.dumps(stored), row["component_id"]),
        )

    before = session_state(deps)
    assert_refused_and_unchanged(client, deps, token, {"option_id": served}, before)


def test_a_genuinely_pre_alias_component_still_works(client, deps, experience_id):
    """Canonical identifiers, positively identified, still accepted."""
    from learning_studio.service import AliasState

    token, _ = open_session(client, experience_id)
    canonical = example("multiple_choice")["content"]["options"][0]["id"]

    object.__setattr__(deps, "component_aliases", alias_state(AliasState.CANONICAL))
    object.__setattr__(
        deps,
        "load_experience",
        lambda principal, experience: _legacy_bundle(deps, principal, experience),
    )

    accepted = client.post(
        "/api/session/answer",
        json={"component_id": "q-one", "response": {"option_id": canonical}},
        headers=session_headers(token),
    )

    assert accepted.status_code == 200


def test_an_alias_belonging_to_another_component_is_refused(
    client, deps, hermes_home, principal, config
):
    """A valid alias, from the wrong card. Aliases are per component."""
    experience_id = service.prepare_experience(
        principal=principal,
        manifest=manifest(
            [example("multiple_choice", id="q-one"), example("multiple_choice", id="q-two")]
        ),
        config=config,
    )["experience_id"]
    token, _ = open_session(client, experience_id)

    other = deps.component_aliases(deps.principal(USER_ID), experience_id, "q-two")
    borrowed = next(iter(other.mapping))
    before = session_state(deps)

    assert_refused_and_unchanged(client, deps, token, {"option_id": borrowed}, before)


def test_no_refusal_reveals_whether_the_evaluator_row_exists(
    client, deps, hermes_home, principal, config
):
    """A missing row, a damaged one and an incomplete map are one message."""
    from learning_studio.service import AliasState

    token, _ = open_session(client, _prepared(principal, config))
    served = served_option(client, token)

    bodies = set()
    for state in (AliasState.UNRESOLVED, AliasState.ALIASED):
        object.__setattr__(deps, "component_aliases", alias_state(state, {}))
        bodies.add(
            client.post(
                "/api/session/answer",
                json={"component_id": "q-one", "response": {"option_id": served}},
                headers=session_headers(token),
            ).text
        )

    assert len(bodies) == 1, "the refusal distinguishes one alias state from another"


def _prepared(principal, config) -> str:
    return service.prepare_experience(
        principal=principal,
        manifest=manifest([example("multiple_choice", id="q-one")]),
        config=config,
    )["experience_id"]


def _legacy_bundle(deps, principal, experience_id):
    """A delivery bundle whose components carry canonical identifiers.

    What a stored experience looked like before identifiers were aliased.
    """
    bundle = service.delivery_bundle(
        principal=principal, experience_id=experience_id, config=deps.config
    )
    for component in bundle.experience["components"]:
        if component["type"] == "multiple_choice":
            component["payload"]["content"] = example("multiple_choice")["content"]
    return bundle


@pytest.mark.parametrize(
    "corrupt_value",
    [
        pytest.param(None, id="null"),
        pytest.param(42, id="integer"),
        pytest.param(True, id="boolean"),
        pytest.param({"nested": "bad"}, id="object"),
    ],
)
def test_a_malformed_alias_value_is_refused_and_changes_nothing(
    client, deps, hermes_home, principal, config, corrupt_value
):
    """The reported probe: these returned 200 and recorded the coerced value.

    `str(canonical)` made each of them identifier-shaped — `"None"`, `"42"`,
    `"True"`, a `repr` — and the session advanced with a stored answer naming
    nothing in the answer key.
    """
    from learning_studio import storage

    experience_id = _prepared(principal, config)
    token, _ = open_session(client, experience_id)
    served = served_option(client, token)

    with storage.connect(config) as conn:
        row = conn.execute(
            "SELECT component_id, evaluation FROM experience_component_evaluations"
        ).fetchone()
        stored = json.loads(row["evaluation"])
        stored["aliases"][next(iter(stored["aliases"]))] = corrupt_value
        conn.execute(
            "UPDATE experience_component_evaluations SET evaluation = ? WHERE component_id = ?",
            (json.dumps(stored), row["component_id"]),
        )

    before = session_state(deps)
    refused = assert_refused_and_unchanged(client, deps, token, {"option_id": served}, before)

    # And nothing about the damage is described to the caller.
    for leaked in ("None", "42", "True", "nested"):
        assert leaked not in refused.json()["error"]


def test_an_explicitly_null_scheme_is_refused_while_an_absent_one_still_works(
    client, deps, hermes_home, principal, config
):
    """Presence and value are different questions.

    An absent `alias_scheme` is the previous release's format and must keep
    working; an explicit `null` is a damaged record and must not.
    """
    from learning_studio import storage

    def rewrite(experience_id: str, change) -> None:
        with storage.connect(config) as conn:
            row = conn.execute(
                "SELECT e.component_id, e.evaluation"
                "  FROM experience_components AS c"
                "  JOIN experience_component_evaluations AS e ON e.component_id = c.id"
                " WHERE c.experience_id = ?",
                (experience_id,),
            ).fetchone()
            stored = json.loads(row["evaluation"])
            change(stored)
            conn.execute(
                "UPDATE experience_component_evaluations SET evaluation = ? WHERE component_id = ?",
                (json.dumps(stored), row["component_id"]),
            )

    canonical = {entry["id"] for entry in example("multiple_choice")["content"]["options"]}

    def previous_head(stored: dict) -> None:
        stored.pop("alias_scheme")
        stored.pop("canonical_identifiers")

    absent = _prepared(principal, config)
    rewrite(absent, previous_head)
    token, _ = open_session(client, absent)
    accepted = client.post(
        "/api/session/answer",
        json={"component_id": "q-one", "response": {"option_id": served_option(client, token)}},
        headers=session_headers(token),
    )
    assert accepted.status_code == 200
    assert next(iter(deps.sessions._sessions.values())).answers["q-one"]["option_id"] in canonical

    explicit_null = _prepared(principal, config)
    rewrite(explicit_null, lambda stored: stored.__setitem__("alias_scheme", None))
    other_token, _ = open_session(client, explicit_null)
    served = served_option(client, other_token)
    refused = client.post(
        "/api/session/answer",
        json={"component_id": "q-one", "response": {"option_id": served}},
        headers=session_headers(other_token),
    )
    assert refused.status_code == 400
    assert refused.json()["error"] == INVALID_RESPONSE_MESSAGE


# ── Alias integrity, through the real route with real stored damage ───────
#
# Nothing here mocks `component_aliases`. Each test corrupts the stored evaluator
# record the way a bug or a tampered database would, then drives the real API —
# because the defect being closed was precisely that a syntactically valid record
# sailed through every layer.


def _damage_stored_component(config, experience_id: str, change, component_key: str = "q-one"):
    from learning_studio import storage

    with storage.connect(config) as conn:
        row = conn.execute(
            "SELECT e.component_id, e.evaluation"
            "  FROM experience_components AS c"
            "  JOIN experience_component_evaluations AS e ON e.component_id = c.id"
            " WHERE c.experience_id = ? AND c.component_key = ?",
            (experience_id, component_key),
        ).fetchone()
        stored = json.loads(row["evaluation"])
        change(stored)
        conn.execute(
            "UPDATE experience_component_evaluations SET evaluation = ? WHERE component_id = ?",
            (json.dumps(stored), row["component_id"]),
        )


@pytest.mark.parametrize(
    ("name", "change"),
    [
        pytest.param(
            "unknown-target",
            lambda stored: stored.__setitem__(
                "aliases", {alias: "unknown" for alias in stored["aliases"]}
            ),
            id="every-target-unknown",
        ),
        pytest.param(
            "one-unknown-target",
            lambda stored: stored["aliases"].__setitem__(next(iter(stored["aliases"])), "unknown"),
            id="one-target-unknown",
        ),
        pytest.param(
            "duplicate-target",
            lambda stored: stored.__setitem__(
                "aliases",
                {alias: next(iter(stored["aliases"].values())) for alias in stored["aliases"]},
            ),
            id="every-alias-one-target",
        ),
        pytest.param(
            "missing-key",
            lambda stored: stored["aliases"].pop(next(iter(stored["aliases"]))),
            id="mapping-missing-a-served-alias",
        ),
        pytest.param(
            "extra-key",
            lambda stored: stored["aliases"].__setitem__("xdeadbeefdeadbeef", "matrix"),
            id="mapping-has-an-unserved-alias",
        ),
        pytest.param(
            "mismatched-inventory",
            lambda stored: stored["canonical_identifiers"].append("extra"),
            id="inventory-does-not-match",
        ),
        pytest.param(
            "inventory-removed",
            lambda stored: stored.pop("canonical_identifiers"),
            id="scheme-2-without-an-inventory",
        ),
    ],
)
def test_a_corrupt_alias_record_is_refused_through_the_real_route(
    client, deps, hermes_home, principal, config, name: str, change
):
    """The reported reproductions, and every neighbouring shape.

    Both reported probes returned HTTP 200, recorded the value, and advanced the
    session. Each must now be a state-neutral 400.
    """
    experience_id = _prepared(principal, config)
    token, _ = open_session(client, experience_id)
    served = served_option(client, token)

    _damage_stored_component(config, experience_id, change)
    before = session_state(deps)

    refused = assert_refused_and_unchanged(client, deps, token, {"option_id": served}, before)

    # Nothing about the component or the damage is described to the caller.
    for leaked in ("unknown", "canonical", "alias", "matrix", "cytosol", served):
        assert leaked not in refused.json()["error"]


def test_a_cross_component_mapping_is_refused_through_the_real_route(
    client, deps, hermes_home, principal, config
):
    """A mapping that is valid — for the other card in the same experience."""
    from learning_studio import storage

    experience_id = service.prepare_experience(
        principal=principal,
        manifest=manifest(
            [example("multiple_choice", id="q-one"), example("multi_select", id="q-two")]
        ),
        config=config,
    )["experience_id"]
    token, _ = open_session(client, experience_id)
    served = served_option(client, token)

    with storage.connect(config) as conn:
        other = json.loads(
            conn.execute(
                "SELECT e.evaluation FROM experience_components AS c"
                "  JOIN experience_component_evaluations AS e ON e.component_id = c.id"
                " WHERE c.experience_id = ? AND c.component_key = ?",
                (experience_id, "q-two"),
            ).fetchone()["evaluation"]
        )

    _damage_stored_component(
        config,
        experience_id,
        lambda stored: stored.update(
            aliases=other["aliases"], canonical_identifiers=other["canonical_identifiers"]
        ),
    )
    before = session_state(deps)

    assert_refused_and_unchanged(client, deps, token, {"option_id": served}, before)


def test_a_valid_current_mapping_still_resolves_through_the_real_route(
    client, deps, hermes_home, principal, config
):
    """The mirror. Proving integrity must not make the ordinary case fail."""
    experience_id = _prepared(principal, config)
    token, _ = open_session(client, experience_id)
    served = served_option(client, token)
    canonical = {entry["id"] for entry in example("multiple_choice")["content"]["options"]}

    accepted = client.post(
        "/api/session/answer",
        json={"component_id": "q-one", "response": {"option_id": served}},
        headers=session_headers(token),
    )

    assert accepted.status_code == 200
    assert next(iter(deps.sessions._sessions.values())).answers["q-one"]["option_id"] in canonical


def test_a_scheme_one_record_still_resolves_through_the_real_route(
    client, deps, hermes_home, principal, config
):
    """An experience prepared under the mapping-only scheme keeps working."""
    experience_id = _prepared(principal, config)
    token, _ = open_session(client, experience_id)
    served = served_option(client, token)
    canonical = {entry["id"] for entry in example("multiple_choice")["content"]["options"]}

    def to_scheme_one(stored: dict) -> None:
        stored["alias_scheme"] = 1
        del stored["canonical_identifiers"]

    _damage_stored_component(config, experience_id, to_scheme_one)

    accepted = client.post(
        "/api/session/answer",
        json={"component_id": "q-one", "response": {"option_id": served}},
        headers=session_headers(token),
    )

    assert accepted.status_code == 200
    assert next(iter(deps.sessions._sessions.values())).answers["q-one"]["option_id"] in canonical


def test_a_corrupt_record_does_not_block_an_identifier_free_response(
    client, deps, hermes_home, principal, config
):
    """A response naming no identifier needs no mapping, and never did.

    `true_false` submits a bare boolean and `{"skipped": true}` names nothing, so
    failing closed on alias resolution must not make those unanswerable — the
    resolver is only ever reached for an identifier that needs translating.
    """
    experience_id = service.prepare_experience(
        principal=principal,
        manifest=manifest([example("true_false", id="q-one")]),
        config=config,
    )["experience_id"]
    token, _ = open_session(client, experience_id)

    _damage_stored_component(
        config, experience_id, lambda stored: stored.__setitem__("aliases", {"bogus": "x"})
    )

    accepted = client.post(
        "/api/session/answer",
        json={"component_id": "q-one", "response": {"value": True}},
        headers=session_headers(token),
    )

    assert accepted.status_code == 200


def test_a_skip_is_accepted_whatever_the_alias_record_says(
    client, deps, hermes_home, principal, config
):
    experience_id = _prepared(principal, config)
    token, _ = open_session(client, experience_id)

    _damage_stored_component(
        config, experience_id, lambda stored: stored.__setitem__("aliases", {"bogus": "x"})
    )

    accepted = client.post(
        "/api/session/answer",
        json={"component_id": "q-one", "response": {"skipped": True}},
        headers=session_headers(token),
    )

    assert accepted.status_code == 200


def test_every_corrupt_state_produces_one_indistinguishable_refusal(
    client, deps, hermes_home, principal, config
):
    """A learner must not be able to tell which invariant failed."""
    bodies = set()
    statuses = set()

    for change in (
        lambda stored: stored.__setitem__(
            "aliases", {alias: "unknown" for alias in stored["aliases"]}
        ),
        lambda stored: stored["aliases"].pop(next(iter(stored["aliases"]))),
        lambda stored: stored.pop("canonical_identifiers"),
        lambda stored: stored.pop("aliases"),
    ):
        experience_id = _prepared(principal, config)
        token, _ = open_session(client, experience_id)
        served = served_option(client, token)
        _damage_stored_component(config, experience_id, change)
        refused = client.post(
            "/api/session/answer",
            json={"component_id": "q-one", "response": {"option_id": served}},
            headers=session_headers(token),
        )
        statuses.add(refused.status_code)
        bodies.add(refused.text)

    assert statuses == {400}
    assert len(bodies) == 1, "the refusal distinguishes one damaged state from another"
