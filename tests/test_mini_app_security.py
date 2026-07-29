"""Body limits, rate limiting, redaction, and the shape of a submitted answer.

Unit-level counterparts to ``tests/test_mini_app_api.py``: the same rules, but
checked directly so a failure names the rule rather than a route.
"""

from __future__ import annotations

import logging

import pytest

from learning_studio.web.dependencies import bot_token_from_env, user_log_reference
from learning_studio.web.security import (
    LOGGABLE_FIELDS,
    SECURITY_HEADERS,
    InvalidResponseValue,
    RateLimited,
    RateLimiter,
    RequestTooLarge,
    enforce_body_limit,
    log_request,
    redacted,
    validate_response_value,
)
from tests.init_data import BOT_TOKEN, USER_ID, build_init_data


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ── Body limits ───────────────────────────────────────────────────────────


def test_a_body_within_the_limit_is_accepted():
    enforce_body_limit("4", b"abcd", 16)


def test_a_measured_oversize_body_is_refused():
    with pytest.raises(RequestTooLarge):
        enforce_body_limit(None, b"x" * 17, 16)


def test_a_declared_oversize_body_is_refused_before_it_is_read():
    """The declaration is what lets a huge upload be refused early."""
    with pytest.raises(RequestTooLarge):
        enforce_body_limit("1000000", b"", 16)


def test_a_malformed_content_length_is_refused():
    with pytest.raises(RequestTooLarge):
        enforce_body_limit("not-a-number", b"{}", 16)


# ── Rate limiting ─────────────────────────────────────────────────────────


def test_requests_within_the_allowance_pass():
    clock = Clock()
    limiter = RateLimiter(limit=3, window_seconds=60, clock=clock)

    for _ in range(3):
        limiter.check("user:1001")


def test_the_allowance_runs_out():
    clock = Clock()
    limiter = RateLimiter(limit=2, window_seconds=60, clock=clock)
    limiter.check("user:1001")
    limiter.check("user:1001")

    with pytest.raises(RateLimited):
        limiter.check("user:1001")


def test_the_window_slides():
    clock = Clock()
    limiter = RateLimiter(limit=1, window_seconds=60, clock=clock)
    limiter.check("user:1001")
    clock.advance(61)

    limiter.check("user:1001")


def test_one_caller_cannot_exhaust_anothers_allowance():
    clock = Clock()
    limiter = RateLimiter(limit=1, window_seconds=60, clock=clock)
    limiter.check("user:1001")

    limiter.check("user:2002")


def test_retry_after_is_a_usable_number_of_seconds():
    clock = Clock()
    limiter = RateLimiter(limit=1, window_seconds=60, clock=clock)
    limiter.check("user:1001")

    with pytest.raises(RateLimited) as caught:
        limiter.check("user:1001")

    assert 1 <= caught.value.retry_after <= 61


def test_the_limiter_does_not_grow_without_bound():
    clock = Clock()
    limiter = RateLimiter(limit=5, window_seconds=10, clock=clock)

    for index in range(500):
        clock.advance(1)
        limiter.check(f"user:{index}")

    assert len(limiter._hits) < 50


# ── Answer shapes ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["b", 3, 3.5, True, None, ["a", "b"], {"selected": ["a"]}, []])
def test_ordinary_answers_are_accepted(value):
    assert validate_response_value(value) == value


def test_an_overlong_answer_is_refused():
    with pytest.raises(InvalidResponseValue):
        validate_response_value("x" * 4001)


def test_an_answer_with_too_many_items_is_refused():
    with pytest.raises(InvalidResponseValue):
        validate_response_value(["x"] * 101)


def test_a_deeply_nested_answer_is_refused():
    nested: object = "bottom"
    for _ in range(8):
        nested = {"next": nested}

    with pytest.raises(InvalidResponseValue):
        validate_response_value(nested)


def test_a_non_json_answer_is_refused():
    with pytest.raises(InvalidResponseValue):
        validate_response_value(object())


def test_an_overlong_field_name_is_refused():
    with pytest.raises(InvalidResponseValue):
        validate_response_value({"k" * 201: "v"})


# ── Redacted logging ──────────────────────────────────────────────────────


def test_only_allowlisted_fields_are_logged():
    event = redacted(event="x", init_data="secret", session_token="secret", user_id=USER_ID)

    assert set(event.fields) <= LOGGABLE_FIELDS
    assert "secret" not in event.render()
    assert USER_ID not in event.render()


def test_a_credential_shaped_value_is_dropped_even_from_an_allowed_field():
    event = redacted(reason=f"failed for hash={'a' * 64}")

    assert "hash=" not in event.render()


def test_the_log_line_is_structured_json():
    import json

    fields = json.loads(redacted(event="health", status=200, route="/api/health").render())

    assert fields == {"event": "health", "status": 200, "route": "/api/health"}


def test_a_user_reference_is_not_the_user_id():
    reference = user_log_reference(USER_ID)

    assert USER_ID not in reference
    assert reference == user_log_reference(USER_ID)
    assert reference != user_log_reference("2002")


def test_serving_requests_writes_no_secret_to_the_logs(caplog):
    """The end-to-end version: drive the API and read every line back."""
    from fastapi.testclient import TestClient

    from learning_studio.config import LearningStudioConfig
    from learning_studio.sessions import SessionStore
    from learning_studio.web.app import INIT_DATA_HEADER, SESSION_HEADER, create_app
    from learning_studio.web.dependencies import Dependencies

    clock = Clock()
    clock.now = 1_800_000_000.0
    config = LearningStudioConfig()
    deps = Dependencies(
        config=config,
        sessions=SessionStore(ttl_seconds=600, max_sessions=10, clock=clock),
        bot_token=lambda: BOT_TOKEN,
        allowed_users=lambda: frozenset({USER_ID}),
        profile=lambda: "default",
        clock=clock,
    )
    init_data = build_init_data(auth_date=int(clock.now) - 5)

    with caplog.at_level(logging.DEBUG), TestClient(create_app(deps)) as client:
        client.get("/api/health", headers={INIT_DATA_HEADER: init_data})
        client.get("/api/health")  # unauthenticated: the refusal is logged too
        client.get(
            "/api/session/component",
            headers={INIT_DATA_HEADER: init_data, SESSION_HEADER: "a-token-that-does-not-exist"},
        )

    logs = caplog.text
    assert init_data not in logs
    assert BOT_TOKEN not in logs
    assert "a-token-that-does-not-exist" not in logs
    assert USER_ID not in logs


def test_log_request_emits_through_the_package_logger(caplog):
    with caplog.at_level(logging.INFO, logger="learning_studio.web"):
        log_request(event="health", status=200)

    assert '"event": "health"' in caplog.text


# ── There is no way to turn authentication off ────────────────────────────


def test_the_api_source_carries_no_auth_bypass_switch():
    """Injection is the test seam; a flag would be a production foot-gun.

    Parsed, not grepped: the modules below *describe* the absence of a bypass
    in their docstrings, and a text search would read that prose as the thing
    it warns against. Only identifiers and runtime string constants count —
    a name to branch on, or an environment variable to read.
    """
    import ast
    from pathlib import Path

    package = Path(__file__).resolve().parent.parent / "learning_studio"
    suspicious = (
        "skip_auth",
        "disable_auth",
        "auth_disabled",
        "bypass",
        "insecure",
        "dev_mode",
        "devmode",
        "testing",
        "allow_anonymous",
        "no_auth",
        "unauthenticated_ok",
    )
    sources = [package / "telegram_auth.py", package / "authorization.py", package / "sessions.py"]
    sources += sorted((package / "web").rglob("*.py"))

    offenders: list[str] = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        }
        symbols: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                symbols.append(node.id)
            elif isinstance(node, ast.Attribute):
                symbols.append(node.attr)
            elif isinstance(node, (ast.arg, ast.keyword)) and node.arg:
                symbols.append(node.arg)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols.append(node.name)
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                symbols.append(node.value)
        offenders += [
            f"{path.name}: {symbol}"
            for symbol in symbols
            for token in suspicious
            if token in symbol.lower()
        ]

    assert offenders == []


def test_the_default_wiring_denies_an_unconfigured_deployment(monkeypatch, tmp_path):
    """``create_app()`` with no injection: no token, no allowlist, no entry."""
    from fastapi.testclient import TestClient

    from learning_studio.web.app import INIT_DATA_HEADER, create_app

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)

    with TestClient(create_app()) as client:
        response = client.get(
            "/api/health",
            headers={INIT_DATA_HEADER: build_init_data(auth_date=1_800_000_000)},
        )

    assert response.status_code == 401


def test_a_configured_token_without_an_allowlist_still_denies(monkeypatch, tmp_path):
    """Being able to prove who you are is not permission to be here."""
    from fastapi.testclient import TestClient

    from learning_studio.web.app import INIT_DATA_HEADER, create_app

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    clock = Clock()
    clock.now = 1_800_000_000.0

    with TestClient(create_app()) as client:
        import time as time_module

        monkeypatch.setattr(time_module, "time", clock)
        response = client.get(
            "/api/health",
            headers={INIT_DATA_HEADER: build_init_data(auth_date=int(clock.now))},
        )

    assert response.status_code in (401, 403)


# ── Secrets in, nothing out ───────────────────────────────────────────────


def test_the_bot_token_is_read_from_the_environment_only():
    assert bot_token_from_env({"TELEGRAM_BOT_TOKEN": " token "}) == "token"
    assert bot_token_from_env({}) == ""


def test_the_security_headers_carry_no_secret_or_host_detail():
    rendered = " ".join(f"{k}: {v}" for k, v in SECURITY_HEADERS.items())

    assert BOT_TOKEN not in rendered
    assert "telegram.org" in rendered  # the one external origin, and it is a frame ancestor
