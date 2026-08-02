"""One learner, one sentence of intent, all the way to a closed runtime.

This is the whole feature in one test, and it is here because every other test
in the suite is about a part. A part can be right while the seams between them
are wrong: a grant bound to the wrong generation, a session the results tool
cannot see, a rollback that leaves a learner holding a button.

What is real here: the manifest validator, the store, the ownership challenge,
the grant store, the session store, the Mini App API with its real Telegram
``initData`` verification, and the response contract. Nothing is stubbed except
the three things a test must not do — start a process, open a tunnel, or send a
Telegram message — and each of those is substituted at the narrowest point.

Nothing in this file touches the network, Telegram, Cloudflare, DNS, a real
profile, or an unrelated process.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from learning_studio import launch as launch_module
from learning_studio import service
from learning_studio.config import LearningStudioConfig
from learning_studio.consent import ConsentLedger
from learning_studio.runtime import bootstrap, ownership, state, supervisor
from learning_studio.runtime import grants as grants_module
from learning_studio.sessions import SessionStore
from learning_studio.web.app import INIT_DATA_HEADER, SESSION_HEADER, create_app
from learning_studio.web.dependencies import Dependencies
from tests.component_examples import example, manifest
from tests.init_data import BOT_TOKEN, OTHER_USER_ID, USER_ID, build_init_data
from tests.served_responses import response_for

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="the runtime owns processes through POSIX primitives only"
)

NOW = 1_800_000_000
TUNNEL_URL = "https://calm-forest-1234.trycloudflare.com"


class Clock:
    def __init__(self, now: float = float(NOW)) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Sent:
    chat_id: str
    url: str
    label: str
    title: str


class Telegram:
    """The narrowest possible stand-in: one function, recording one message."""

    def __init__(self) -> None:
        self.messages: list[Sent] = []

    def __call__(self, *, destination, url, label, title):
        self.messages.append(Sent(destination.chat_id, url, label, title))


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def config() -> LearningStudioConfig:
    return LearningStudioConfig(
        runtime_idle_timeout_seconds=1800,
        runtime_max_lifetime_seconds=7200,
        runtime_graceful_stop_seconds=1,
        launch_button_label="Open Learning Studio",
        mini_app_allowed_telegram_users=(USER_ID,),
    )


@pytest.fixture
def learner_session(monkeypatch):
    """The private Telegram conversation the learner is speaking in."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", USER_ID)
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", USER_ID)
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "private")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", USER_ID)


@pytest.fixture
def grants(clock) -> grants_module.GrantStore:
    return grants_module.GrantStore(profile="default", generation=1, clock=clock)


@pytest.fixture
def runtime(clock, grants, monkeypatch, hermes_home, config):
    """A runtime that answers the challenge and serves the real grant store."""
    stopped = {"value": False}

    def control(record, method, path, *, body=None, timeout=None):
        if stopped["value"]:
            raise ownership.ControlError("control_unreachable_OSError")
        if path == ownership.STATUS_PATH:
            return {
                "runtime_id": record.runtime_id,
                "generation": record.generation,
                "pid": record.pid,
                "executable": record.executable,
                "started_at": float(NOW),
                "idle_seconds": None,
                "server_state": "ready",
                "tunnel_state": "ready",
                "tunnel_ready": True,
                "tunnel_url": TUNNEL_URL,
                "sessions": len(grants),
                "expires_in_seconds": 7200,
            }
        if path == ownership.SHUTDOWN_PATH:
            stopped["value"] = True
            return {}
        if path == ownership.GRANT_PATH:
            return grants.create(body or {})
        if path == ownership.GRANT_REVOKE_PATH:
            return {"revoked": grants.revoke(str((body or {}).get("launch_id", "")))}
        if path == ownership.LAUNCH_PATH:
            return grants.progress(body or {})
        raise ownership.ControlError("control_status_404")

    monkeypatch.setattr(ownership, "_request", control)
    monkeypatch.setattr(bootstrap, "is_bootstrapped", lambda: True)
    monkeypatch.setattr(supervisor, "resolve_cloudflared", lambda cfg: "/usr/bin/cloudflared")
    state.write_record(
        state.RuntimeRecord(
            runtime_id="runtime-e2e",
            generation=1,
            profile="default",
            pid=4242,
            host="127.0.0.1",
            port=45678,
            control_token="control-secret",
            executable="/plugin/venv/bin/python",
            started_at=float(NOW),
            idle_timeout_seconds=1800,
            max_lifetime_seconds=7200,
        )
    )
    return stopped


@pytest.fixture
def webview(clock, config, grants, hermes_home):
    """The Mini App, exactly as the runtime serves it, over the real API."""
    from fastapi.testclient import TestClient

    sessions = SessionStore(
        ttl_seconds=config.mini_app_session_ttl_seconds,
        max_sessions=config.mini_app_max_sessions,
        clock=clock,
    )
    dependencies = Dependencies(
        config=config,
        sessions=sessions,
        bot_token=lambda: BOT_TOKEN,
        allowed_users=lambda: frozenset({USER_ID}),
        profile=lambda: "default",
        clock=clock,
        grants=grants,
        load_experience=lambda principal, experience_id: service.delivery_bundle(
            principal=principal, experience_id=experience_id, config=config
        ),
        load_asset=lambda principal, asset_id: service.read_managed_asset(
            principal=principal, asset_id=asset_id, config=config
        ),
        component_aliases=lambda principal, experience_id, key: service.component_aliases(
            principal=principal, experience_id=experience_id, component_key=key, config=config
        ),
        reveal_answer=lambda principal, experience_id, key: service.reveal_component_answer(
            principal=principal, experience_id=experience_id, component_key=key, config=config
        ),
    )
    with TestClient(create_app(dependencies)) as client:
        yield client


def auth(user_id: str = USER_ID, age: int = 5) -> dict[str, str]:
    return {INIT_DATA_HEADER: build_init_data(user_id=user_id, auth_date=NOW - age)}


# ── The whole thing ───────────────────────────────────────────────────────


def test_a_learner_asks_to_practise_and_gets_a_working_exercise(
    hermes_home, principal, config, runtime, grants, learner_session, webview, clock
):
    """From "quiz me on this" to a stopped runtime, with nothing invented.

    The narrative, in order, is the whole point of reading this test:

    1. the agent records what it learned about the learner;
    2. it designs an exercise and stores it as validated data;
    3. it opens the exercise, because the learner asked;
    4. one button reaches one private chat;
    5. the learner opens it — and the API mints a session only because a grant
       exists for that account and that exercise;
    6. they answer a question, and the answer is recorded for the session only;
    7. the agent asks how it went and is told progress, not marks;
    8. the runtime stops, and everything it held goes with it.
    """
    from learning_studio import tools

    # 1. What the conversation taught the agent. Nothing here is a score.
    saved = json.loads(
        tools.handle_save_context({"temporary_context": {"subject": "photosynthesis"}})
    )
    assert saved["ok"] is True
    assert saved["hermes_memory_updated"] is False

    # 2. The exercise, designed by the agent and validated by the plugin.
    prepared = json.loads(
        tools.handle_prepare(
            {
                "manifest": manifest(
                    [
                        example("multiple_choice", id="q-one"),
                        example("short_answer", id="q-two"),
                    ]
                )
            }
        )
    )
    assert prepared["ok"] is True
    experience_id = prepared["experience_id"]
    # Preparing stores; it does not open. The response says so, and says what
    # to call next rather than leaving the agent to guess.
    assert prepared["stored"] is True
    assert "nothing has been launched" in prepared["delivery"].lower()
    assert "learning_studio_launch" in prepared["delivery"]

    # 3 and 4. The launch. One message, to the chat the session named.
    telegram = Telegram()
    launched = launch_module.launch_experience(
        principal=principal,
        experience_id=experience_id,
        initiation="learner_request",
        config=config,
        deliver=telegram,
        ledger=ConsentLedger(clock=clock),
    )
    assert launched["button_delivered"] is True
    assert len(telegram.messages) == 1
    assert telegram.messages[0].chat_id == USER_ID
    assert telegram.messages[0].url == TUNNEL_URL
    assert telegram.messages[0].label == "Open Learning Studio"

    # The address the learner was sent never reaches the agent.
    assert "trycloudflare" not in json.dumps(launched)

    # 5. The learner taps it. Real initData, real HMAC, real session.
    opened = webview.post("/api/session", headers=auth(), json={"experience_id": experience_id})
    assert opened.status_code == 201
    token = opened.json()["session_token"]
    assert opened.json()["experience"]["component_count"] == 2

    # 6. One answer, recorded for the session and marked by nobody.
    component = webview.get(
        "/api/session/component", headers={**auth(), SESSION_HEADER: token}
    ).json()["component"]
    answered = webview.post(
        "/api/session/answer",
        headers={**auth(), SESSION_HEADER: token},
        json={
            "component_id": component["component_id"],
            "response": response_for(component["type"], component["payload"]["content"]),
        },
    )
    assert answered.status_code == 200
    assert answered.json()["scored"] is False

    # 7. What the agent is allowed to know afterwards.
    results = launch_module.launch_results(
        principal=principal, experience_id=experience_id, config=config
    )
    assert results["opened"] is True
    assert results["answered"] == 1
    assert results["completed"] is False
    assert results["scored"] is False
    assert results["attempts_stored"] is False
    assert results["responses_returned"] is False
    assert results["memory_candidates"] == []

    # 8. Closing time.
    from learning_studio.runtime import manager

    stopped = manager.stop(config)
    assert stopped["stopped"] is True
    assert state.read_record() is None


def test_the_agent_is_never_told_what_the_learner_wrote(
    hermes_home, principal, config, runtime, grants, learner_session, webview, clock
):
    """Progress is the plugin's to report; the work is the learner's to share."""
    prepared = service.prepare_experience(
        principal=principal,
        manifest=manifest([example("short_answer", id="q-one")]),
        config=config,
    )
    experience_id = prepared["experience_id"]
    launch_module.launch_experience(
        principal=principal,
        experience_id=experience_id,
        initiation="learner_request",
        config=config,
        deliver=Telegram(),
        ledger=ConsentLedger(clock=clock),
    )

    token = webview.post(
        "/api/session", headers=auth(), json={"experience_id": experience_id}
    ).json()["session_token"]
    component = webview.get(
        "/api/session/component", headers={**auth(), SESSION_HEADER: token}
    ).json()["component"]
    webview.post(
        "/api/session/answer",
        headers={**auth(), SESSION_HEADER: token},
        json={
            "component_id": component["component_id"],
            "response": response_for(component["type"], component["payload"]["content"]),
        },
    )

    results = json.dumps(
        launch_module.launch_results(
            principal=principal, experience_id=experience_id, config=config
        )
    )

    # Checked against the field names: the prose deliberately says "attempt" in
    # order to say there is none, and a substring scan of the whole payload
    # would read its own honesty as a leak.
    payload = launch_module.launch_results(
        principal=principal, experience_id=experience_id, config=config
    )
    for field in ("responses", "answers", "attempts", "response", "text"):
        assert field not in payload, field
    assert "word" not in results, "a learner's own words reached the agent"


# ── The gate the button is not ────────────────────────────────────────────


def test_the_tunnel_address_alone_opens_nothing(
    hermes_home, principal, config, runtime, grants, learner_session, webview
):
    """Anybody may find a public URL. Only a launched learner may use it.

    This is what makes it acceptable to put an address in a chat message: the
    address is not the credential. Without a grant, a fully authenticated,
    allowlisted account that genuinely owns the exercise is still refused.
    """
    experience_id = service.prepare_experience(
        principal=principal,
        manifest=manifest([example("true_false", id="q-one")]),
        config=config,
    )["experience_id"]

    refused = webview.post("/api/session", headers=auth(), json={"experience_id": experience_id})

    assert refused.status_code == 404
    assert len(grants) == 0


def test_a_revoked_launch_cannot_be_opened(
    hermes_home, principal, config, runtime, grants, learner_session, webview, clock
):
    """The rollback path, seen from the learner's side."""
    experience_id = service.prepare_experience(
        principal=principal,
        manifest=manifest([example("true_false", id="q-one")]),
        config=config,
    )["experience_id"]

    with pytest.raises(RuntimeError):
        launch_module.launch_experience(
            principal=principal,
            experience_id=experience_id,
            initiation="learner_request",
            config=config,
            deliver=_failing_telegram,
            ledger=ConsentLedger(clock=clock),
        )

    refused = webview.post("/api/session", headers=auth(), json={"experience_id": experience_id})

    assert refused.status_code == 404


def _failing_telegram(**_kwargs):
    raise RuntimeError("telegram is unreachable")


def test_another_account_cannot_use_this_learners_launch(
    hermes_home, principal, config, runtime, grants, learner_session, webview, clock
):
    experience_id = service.prepare_experience(
        principal=principal,
        manifest=manifest([example("true_false", id="q-one")]),
        config=config,
    )["experience_id"]
    launch_module.launch_experience(
        principal=principal,
        experience_id=experience_id,
        initiation="learner_request",
        config=config,
        deliver=Telegram(),
        ledger=ConsentLedger(clock=clock),
    )

    intruder = webview.post(
        "/api/session", headers=auth(user_id=OTHER_USER_ID), json={"experience_id": experience_id}
    )

    # 403 at the allowlist, or 404 at the grant — either way, not in.
    assert intruder.status_code in (403, 404)


# ── Idle cleanup, from the outside ────────────────────────────────────────


def test_an_idle_runtime_is_reported_as_gone_once_it_has_stopped_itself(
    hermes_home, config, runtime, learner_session
):
    """The agent sees "not running", not a stale record it should act on."""
    from learning_studio.runtime import manager

    assert manager.status(config)["running"] is True

    runtime["value"] = True  # the runtime's own idle timer fired and it exited

    after = manager.status(config)

    assert after["running"] is False
    assert after["stale_record"] is True
    assert "trycloudflare" not in json.dumps(after)


def test_stopping_an_already_stopped_runtime_is_a_stated_no_op(
    hermes_home, config, runtime, learner_session
):
    from learning_studio.runtime import manager

    runtime["value"] = True

    outcome = manager.stop(config)

    assert outcome["ok"] is True
    assert outcome["stopped"] is False
    assert outcome["state"] == "not_running"


# ── Nothing left the machine ──────────────────────────────────────────────


def test_this_file_reaches_nothing_outside_the_machine():
    """A guard on the test itself, by import rather than by grep.

    Three things are substituted here — the process, the tunnel, and the
    message — and each at the narrowest point that removes an outside effect.
    What must not creep in is a *real* client of any of them, so this checks
    what the module imports rather than what its own assertions mention.
    """
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    for forbidden in ("socket", "urllib", "httpx", "requests", "subprocess", "telegram"):
        assert forbidden not in imported, forbidden
