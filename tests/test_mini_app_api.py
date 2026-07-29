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
from pathlib import Path

import pytest

from learning_studio import service
from learning_studio.config import LearningStudioConfig
from learning_studio.sessions import SessionStore
from learning_studio.web.app import (
    INIT_DATA_HEADER,
    SESSION_HEADER,
    create_app,
)
from learning_studio.web.dependencies import Dependencies
from tests.component_examples import CANARY, all_canaries, example, manifest
from tests.init_data import BOT_TOKEN, OTHER_USER_ID, USER_ID, build_init_data

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
            json={"component_id": component_id, "response": "an answer"},
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
