"""The launch transaction: consent, grants, reuse, and rollback.

Nothing here starts a process or opens a tunnel. The runtime is a fake that
answers the ownership challenge and serves a real :class:`GrantStore`, which is
the object whose behaviour actually matters — every other part of a launch is
either checked in SQL or already tested where it lives.

No Telegram request is made: delivery is the injected seam, and the test that
covers the shipped default asserts that it *refuses*.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from learning_studio import launch as launch_module
from learning_studio import service
from learning_studio.config import LearningStudioConfig
from learning_studio.consent import ConsentRequired
from learning_studio.identity import Principal
from learning_studio.runtime import bootstrap, ownership, state, supervisor
from learning_studio.runtime import grants as grants_module
from learning_studio.runtime.errors import RuntimeUnavailable
from tests.component_examples import example, manifest

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="the runtime owns processes through POSIX primitives only"
)

TUNNEL_URL = "https://calm-forest-1234.trycloudflare.com"


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def config(**overrides) -> LearningStudioConfig:
    settings = {
        "runtime_readiness_timeout_seconds": 5,
        "runtime_graceful_stop_seconds": 1,
        "runtime_idle_timeout_seconds": 60,
        "runtime_max_lifetime_seconds": 300,
        "mini_app_allowed_telegram_users": ("1001", "2002"),
    }
    settings.update(overrides)
    return LearningStudioConfig(**settings)


class FakeRuntime:
    """A runtime that answers the challenge and serves a real grant store.

    Substituted for the control transport rather than for the supervisor, so
    the ownership challenge, the record, the generation check and the grant
    logic are all the real ones.
    """

    def __init__(self, clock: Clock, *, tunnel_url: str = TUNNEL_URL, generation: int = 1) -> None:
        self.clock = clock
        self.tunnel_url = tunnel_url
        self.generation = generation
        self.store = grants_module.GrantStore(profile="default", generation=generation, clock=clock)
        self.calls: list[str] = []
        self.stopped = False

    def __call__(self, record, method, path, *, body=None, timeout=None):
        self.calls.append(path)
        if self.stopped:
            raise ownership.ControlError("control_unreachable_OSError")
        if path == ownership.STATUS_PATH:
            return {
                "runtime_id": record.runtime_id,
                "generation": record.generation,
                "pid": record.pid,
                "executable": record.executable,
                "started_at": self.clock(),
                "idle_seconds": None,
                "server_state": "ready",
                "tunnel_state": "ready" if self.tunnel_url else "failed",
                "tunnel_ready": bool(self.tunnel_url),
                "tunnel_url": self.tunnel_url,
                "sessions": 0,
                "expires_in_seconds": 300,
            }
        if path == ownership.SHUTDOWN_PATH:
            self.stopped = True
            return {}
        if path == ownership.GRANT_PATH:
            return self.store.create(body or {})
        if path == ownership.GRANT_ACTIVATE_PATH:
            return {"activated": self.store.activate(str((body or {}).get("launch_id", "")))}
        if path == ownership.GRANT_REVOKE_PATH:
            return {"revoked": self.store.revoke(str((body or {}).get("launch_id", "")))}
        if path == ownership.LAUNCH_PATH:
            return self.store.progress(body or {})
        raise ownership.ControlError("control_status_404")


class Deliveries:
    """A stand-in for the Telegram sender, recording what it was asked to send."""

    def __init__(self, *, fails: Exception | None = None) -> None:
        self.fails = fails
        self.sent: list[dict] = []

    def __call__(self, *, destination, url, label, title, launch_id):
        self.sent.append(
            {
                "chat_id": destination.chat_id,
                "url": url,
                "label": label,
                "title": title,
                "launch_id": launch_id,
            }
        )
        if self.fails:
            raise self.fails


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def runtime(clock, monkeypatch, hermes_home) -> FakeRuntime:
    """A running, proved-owned runtime with a validated tunnel."""
    fake = FakeRuntime(clock)
    monkeypatch.setattr(ownership, "_request", fake)
    state.write_record(
        state.RuntimeRecord(
            runtime_id="runtime-1",
            generation=1,
            profile="default",
            pid=4242,
            host="127.0.0.1",
            port=45678,
            control_token="token",
            executable="/x/python",
            started_at=clock(),
            idle_timeout_seconds=60,
            max_lifetime_seconds=300,
        )
    )
    # Present so `require_prerequisites` is satisfied without building one.
    python = bootstrap.runtime_python()
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "is_bootstrapped", lambda: True)
    monkeypatch.setattr(supervisor, "resolve_cloudflared", lambda cfg: "/usr/bin/cloudflared")
    return fake


@pytest.fixture
def telegram_session(gateway_session, monkeypatch):
    """A private Telegram conversation with the learner who owns the exercise.

    The allowlist is part of the fixture because it is part of the precondition:
    with no Hermes gate configured at all, this plugin authorises nobody, and a
    launch is refused before it reaches anything else. That is the correct
    behaviour — an unconfigured deployment is closed — and it is asserted on its
    own further down.
    """
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "1001")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "1001")
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "dm")
    monkeypatch.setenv("HERMES_SESSION_MESSAGE_ID", "555")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "1001,2002")


#: What the learner is holding in every test below. Recorded through the same
#: store the gateway hook writes to, so the quotation a launch supplies is
#: checked against a message the platform "delivered" rather than model say-so.
LEARNER_MESSAGE = "can you quiz me on photosynthesis please"
LEARNER_QUOTE = "quiz me on photosynthesis"
SUGGESTION_REPLY = "go on then, that sounds useful"
SUGGESTION_QUOTE = "go on then"


@pytest.fixture(autouse=True)
def spoken(clock, telegram_session, monkeypatch):
    """An evidence store holding the learner's current message.

    Written the way `pre_gateway_dispatch` writes it: keyed by the exact
    message id the session carries, before the model gets a turn.

    Autouse because *every* launch now needs it. A test about destinations or
    rollback is not a test about consent, and it should not have to restate
    that somebody spoke — but it does have to be true, because a launch with no
    trusted message is refused before it reaches anything else.
    """
    from learning_studio.evidence import EvidenceKey, EvidenceStore

    store = EvidenceStore(clock=clock)
    monkeypatch.setattr("learning_studio.consent.STORE", store)
    store.record(
        EvidenceKey(
            profile="default",
            platform="telegram",
            chat_id="1001",
            thread_id="",
            user_id="1001",
            message_id="555",
        ),
        LEARNER_MESSAGE + " " + SUGGESTION_REPLY,
    )
    return store


@pytest.fixture
def experience_id(hermes_home, principal) -> str:
    result = service.prepare_experience(
        principal=principal,
        manifest=manifest([example("multiple_choice", id="q-one")]),
        config=config(),
    )
    return result["experience_id"]


#: The words the learner is holding in every test below. Recorded by the
#: `spoken` fixture the way the gateway hook records a real message, so the
#: quotation the launch supplies is checked against something the platform
#: "delivered" rather than against the model's say-so.
LEARNER_MESSAGE = "can you quiz me on photosynthesis please"
LEARNER_QUOTE = "quiz me on photosynthesis"


def launch(principal, experience_id, *, deliver, evidence=None, settings=None, **kwargs):
    payload = {"initiation": "learner_request", "learner_quote": LEARNER_QUOTE}
    payload.update(kwargs)
    return launch_module.launch_experience(
        principal=principal,
        experience_id=experience_id,
        config=settings or config(),
        deliver=deliver,
        evidence=evidence,
        **payload,
    )


# ── The happy path ────────────────────────────────────────────────────────


def test_an_explicit_request_opens_the_exercise(
    runtime, telegram_session, principal, experience_id
):
    deliver = Deliveries()

    result = launch(principal, experience_id, deliver=deliver)

    assert result["ok"] is True
    assert result["button_delivered"] is True
    assert len(deliver.sent) == 1
    assert deliver.sent[0]["url"] == TUNNEL_URL
    assert deliver.sent[0]["chat_id"] == "1001"


def test_the_button_carries_the_configured_label(
    runtime, telegram_session, principal, experience_id
):
    deliver = Deliveries()

    launch(
        principal,
        experience_id,
        deliver=deliver,
        settings=config(launch_button_label="Open Aula Lola"),
    )

    assert deliver.sent[0]["label"] == "Open Aula Lola"


def test_the_result_never_carries_the_public_address(
    runtime, telegram_session, principal, experience_id
):
    """The one thing the model must not learn, asserted on the whole payload."""
    result = launch(principal, experience_id, deliver=Deliveries())

    body = json.dumps(result)
    assert "trycloudflare" not in body
    assert "127.0.0.1" not in body
    assert "1001" not in body, "the learner's Telegram id reached the agent"


def test_the_result_says_nothing_is_scored(runtime, telegram_session, principal, experience_id):
    result = launch(principal, experience_id, deliver=Deliveries())

    assert result["scored"] is False
    assert result["attempts_stored"] is False
    assert "no attempt, score, mastery" in result["notice"]


def test_a_grant_is_created_and_bound_to_the_learner(runtime, spoken, principal, experience_id):
    result = launch(principal, experience_id, deliver=Deliveries())

    granted = runtime.store.admit(launch_id=result["launch_id"], telegram_user_id="1001")

    assert granted is not None
    assert granted.experience_id == experience_id
    assert granted.generation == 1


# ── Reuse ─────────────────────────────────────────────────────────────────


def test_a_repeat_launch_reuses_the_grant_and_sends_nothing_new(
    runtime, spoken, principal, experience_id
):
    """A retry reports the launch that is open; it does not send a second button."""
    deliver = Deliveries()

    first = launch(principal, experience_id, deliver=deliver)
    second = launch(principal, experience_id, deliver=deliver)

    assert first["button_delivered"] is True
    assert second["reused_existing_launch"] is True
    assert second["button_delivered"] is False
    assert len(deliver.sent) == 1, "a repeat sent a second button"
    assert first["launch_id"] == second["launch_id"]


def test_a_second_exercise_needs_a_second_message(
    runtime, spoken, principal, hermes_home, monkeypatch
):
    """One message opens one exercise. Two exercises take two messages."""
    deliver = Deliveries()
    first_id = service.prepare_experience(
        principal=principal, manifest=manifest([example("true_false", id="a")]), config=config()
    )["experience_id"]
    second_id = service.prepare_experience(
        principal=principal, manifest=manifest([example("true_false", id="b")]), config=config()
    )["experience_id"]

    first = launch(principal, first_id, deliver=deliver)

    # The learner writes again, and the gateway records the new message.
    from learning_studio.evidence import EvidenceKey

    monkeypatch.setenv("HERMES_SESSION_MESSAGE_ID", "556")
    spoken.record(
        EvidenceKey(
            profile="default",
            platform="telegram",
            chat_id="1001",
            thread_id="",
            user_id="1001",
            message_id="556",
        ),
        "now quiz me on respiration instead",
    )
    second = launch(principal, second_id, deliver=deliver, learner_quote="quiz me on respiration")

    assert first["launch_id"] != second["launch_id"]
    assert len(deliver.sent) == 2


# ── Consent ───────────────────────────────────────────────────────────────
#
# The policy itself is tested in `tests/test_consent_evidence.py`. What these
# cover is how it meets the transaction: a refusal must start nothing, and a
# success must spend the learner's message exactly once and only after the
# button has actually gone.


def test_an_unconfirmed_suggestion_is_refused_before_anything_starts(
    runtime, spoken, principal, experience_id
):
    deliver = Deliveries()

    with pytest.raises(ConsentRequired) as caught:
        launch(
            principal,
            experience_id,
            deliver=deliver,
            evidence=spoken,
            initiation="agent_suggestion",
            learner_quote=SUGGESTION_QUOTE,
        )

    assert caught.value.reason == "suggestion_not_confirmed"
    assert deliver.sent == []
    assert len(runtime.store) == 0


def test_a_launch_quoting_words_nobody_wrote_is_refused(runtime, spoken, principal, experience_id):
    """The headline regression: the model asserts a request that was not made."""
    deliver = Deliveries()

    with pytest.raises(ConsentRequired) as caught:
        launch(
            principal,
            experience_id,
            deliver=deliver,
            evidence=spoken,
            learner_quote="yes open the exercise for me",
        )

    assert caught.value.reason == "quote_not_in_current_message"
    assert deliver.sent == []
    assert len(runtime.store) == 0


def test_a_turn_carrying_no_learner_message_cannot_launch(
    runtime, spoken, principal, experience_id, monkeypatch
):
    """A cron job or background task has nobody to have asked."""
    monkeypatch.delenv("HERMES_SESSION_MESSAGE_ID", raising=False)
    deliver = Deliveries()

    with pytest.raises(ConsentRequired):
        launch(principal, experience_id, deliver=deliver, evidence=spoken)

    assert deliver.sent == []


def test_a_confirmed_suggestion_opens_the_exercise(runtime, spoken, principal, experience_id):
    deliver = Deliveries()

    result = launch(
        principal,
        experience_id,
        deliver=deliver,
        evidence=spoken,
        initiation="agent_suggestion",
        learner_confirmed=True,
        learner_quote=SUGGESTION_QUOTE,
    )

    assert result["button_delivered"] is True
    assert "you read them as" in result["basis"]


def test_the_reported_basis_never_claims_the_learner_agreed(
    runtime, spoken, principal, experience_id
):
    """The response reports a check that passed, not a state of mind."""
    result = launch(principal, experience_id, deliver=Deliveries(), evidence=spoken)

    assert "the learner agreed" not in result["basis"].lower()
    assert "quoted are in the learner's current message" in result["basis"]


def test_one_message_cannot_open_a_second_launch(runtime, spoken, principal, experience_id, clock):
    """The learner said one thing once; it opens one exercise once.

    The first launch spends that message. When the grant it created has lapsed,
    the same words must not quietly open a second public entrance — which is
    exactly what the previous ledger did, because it deleted its own stale
    entry and the next call then read the consent as fresh.
    """
    deliver = Deliveries()
    launch(principal, experience_id, deliver=deliver, evidence=spoken)

    clock.advance(grants_module.DEFAULT_GRANT_TTL_SECONDS + 1)

    with pytest.raises(ConsentRequired) as caught:
        launch(principal, experience_id, deliver=deliver, evidence=spoken)

    assert caught.value.reason == "confirmation_already_used"
    assert len(deliver.sent) == 1


def test_a_spent_message_keeps_failing_on_every_retry(
    runtime, spoken, principal, experience_id, clock
):
    """Not only on the first retry. Stale is a terminal state."""
    launch(principal, experience_id, deliver=Deliveries(), evidence=spoken)
    clock.advance(grants_module.DEFAULT_GRANT_TTL_SECONDS + 1)

    for _ in range(3):
        with pytest.raises(ConsentRequired):
            launch(principal, experience_id, deliver=Deliveries(), evidence=spoken)


def test_an_explicit_request_never_needs_a_confirmation_flag(
    runtime, spoken, principal, experience_id
):
    """Asking to practise *is* the agreement; a second ask is friction."""
    result = launch(principal, experience_id, deliver=Deliveries(), evidence=spoken)

    assert result["button_delivered"] is True
    assert "request to practise" in result["basis"]


def test_a_failure_that_proves_nothing_was_sent_frees_the_message(
    runtime, spoken, principal, experience_id
):
    """No token means no request was made, so a retry may use the same words."""
    from learning_studio.runtime.errors import DELIVERY_FAILED, LaunchRefused

    with pytest.raises(LaunchRefused):
        launch(
            principal,
            experience_id,
            deliver=Deliveries(fails=LaunchRefused(DELIVERY_FAILED, reason="bot_token_absent")),
            evidence=spoken,
        )

    result = launch(principal, experience_id, deliver=Deliveries(), evidence=spoken)

    assert result["button_delivered"] is True


def test_a_failure_that_might_have_sent_something_keeps_the_message_claimed(
    runtime, spoken, principal, experience_id
):
    """A connection that dropped mid-request proves nothing.

    A message may be sitting in the learner's chat. A retry on the same
    sentence would put a second button beside a first one nobody can see, so
    the agent has to go back and ask.
    """
    deliver = Deliveries(fails=RuntimeError("connection reset mid-request"))

    with pytest.raises(RuntimeError):
        launch(principal, experience_id, deliver=deliver, evidence=spoken)

    with pytest.raises(ConsentRequired) as caught:
        launch(principal, experience_id, deliver=Deliveries(), evidence=spoken)

    assert caught.value.reason == "confirmation_already_used"


# ── Rollback ──────────────────────────────────────────────────────────────


def test_a_failed_delivery_leaves_no_grant_behind(
    runtime, telegram_session, principal, experience_id
):
    """A learner must never hold an entrance to an exercise nobody was told about."""
    with pytest.raises(RuntimeError):
        launch(
            principal,
            experience_id,
            deliver=Deliveries(fails=RuntimeError("telegram is down")),
        )

    assert len(runtime.store) == 0


def test_a_failed_delivery_does_not_stop_a_runtime_it_reused(
    runtime, telegram_session, principal, experience_id
):
    """A reused runtime may be serving somebody else's session."""
    with pytest.raises(RuntimeError):
        launch(
            principal,
            experience_id,
            deliver=Deliveries(fails=RuntimeError("telegram is down")),
        )

    assert runtime.stopped is False
    assert state.read_record() is not None


def test_a_runtime_without_a_tunnel_refuses_and_sends_nothing(
    clock, monkeypatch, hermes_home, telegram_session, principal, experience_id
):
    fake = FakeRuntime(clock, tunnel_url="")
    monkeypatch.setattr(ownership, "_request", fake)
    monkeypatch.setattr(bootstrap, "is_bootstrapped", lambda: True)
    monkeypatch.setattr(supervisor, "resolve_cloudflared", lambda cfg: "/usr/bin/cloudflared")
    state.write_record(
        state.RuntimeRecord(
            runtime_id="runtime-1",
            generation=1,
            profile="default",
            pid=4242,
            host="127.0.0.1",
            port=45678,
            control_token="token",
            executable="/x/python",
            started_at=clock(),
            idle_timeout_seconds=60,
            max_lifetime_seconds=300,
        )
    )
    deliver = Deliveries()

    with pytest.raises(RuntimeUnavailable) as caught:
        launch(principal, experience_id, deliver=deliver)

    assert caught.value.reason == "tunnel_not_ready"
    assert deliver.sent == []


# ── Isolation ─────────────────────────────────────────────────────────────


def test_another_learner_cannot_launch_this_exercise(
    runtime, monkeypatch, hermes_home, other_principal, experience_id, telegram_session
):
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "2002")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "2002")
    deliver = Deliveries()

    with pytest.raises(service.NotFoundError):
        launch(other_principal, experience_id, deliver=deliver)

    assert deliver.sent == []
    assert len(runtime.store) == 0


def test_another_learner_cannot_read_these_results(
    runtime, monkeypatch, hermes_home, principal, other_principal, experience_id, telegram_session
):
    launch(principal, experience_id, deliver=Deliveries())

    with pytest.raises(service.NotFoundError):
        launch_module.launch_results(
            principal=other_principal, experience_id=experience_id, config=config()
        )


# ── Results ───────────────────────────────────────────────────────────────


def test_results_report_a_launch_nobody_opened(runtime, telegram_session, principal, experience_id):
    launch(principal, experience_id, deliver=Deliveries())

    result = launch_module.launch_results(
        principal=principal, experience_id=experience_id, config=config()
    )

    assert result["state"] == "waiting"
    assert result["opened"] is False
    assert result["completed"] is False
    # How many questions there are is known from the stored exercise; how far
    # they got is not, because there is no session yet.
    assert result["component_count"] == 1
    assert result["position"] == 0
    assert result["answered"] == 0


def test_results_never_invent_a_score_or_return_an_answer(
    runtime, telegram_session, principal, experience_id
):
    launch(principal, experience_id, deliver=Deliveries())

    result = launch_module.launch_results(
        principal=principal, experience_id=experience_id, config=config()
    )

    assert result["scored"] is False
    assert result["attempts_stored"] is False
    assert result["responses_returned"] is False
    assert result["memory_candidates"] == []

    # Checked against the *field names*, not the prose: the notice deliberately
    # uses the words "score" and "mark" in order to say there are none, and a
    # substring search over the whole payload would read that as a violation.
    for invented in ("score", "grade", "mark", "mastery", "percent", "review_due", "next_review"):
        assert invented not in result, invented


def test_results_with_no_runtime_say_unknown_rather_than_no(
    hermes_home, telegram_session, principal, experience_id, monkeypatch
):
    """`False` is a finding; this is an absence of evidence, and says so.

    The runtime that would have held the answer is gone. Reporting `opened:
    false` would state as fact something merely unobservable, and an agent
    reading it would tell the learner they had ignored an exercise nobody can
    show they ever saw.
    """
    result = launch_module.launch_results(
        principal=principal, experience_id=experience_id, config=config()
    )

    assert result["availability"] == "unavailable"
    assert result["state"] == "not_running"
    assert result["opened"] is None
    assert result["completed"] is None
    assert "cannot be determined" in result["message"]


def test_results_reflect_a_session_that_was_actually_opened(
    runtime, telegram_session, principal, experience_id
):
    """Progress is read from the live session, never from a second copy of it."""
    result = launch(principal, experience_id, deliver=Deliveries())
    granted = runtime.store.admit(launch_id=result["launch_id"], telegram_user_id="1001")

    class Session:
        position = 1
        component_count = 3
        answers = {"q-one": {}}
        revealed: dict = {}
        completed = False
        completed_at = None
        expires_at = 1e12

    runtime.store.admit_session(granted, lambda: ("token", Session()))

    result = launch_module.launch_results(
        principal=principal, experience_id=experience_id, config=config()
    )

    assert result["state"] == "opened"
    assert result["opened"] is True
    assert result["position"] == 1
    assert result["answered"] == 1
    assert result["completed"] is False


# ── The shipped delivery seam ─────────────────────────────────────────────


def test_the_shipped_delivery_is_the_telegram_sender(hermes_home, monkeypatch):
    """No test injects it, so this is what proves the default is wired at all."""
    sent: list[dict] = []
    monkeypatch.setattr(
        "learning_studio.telegram_launch.deliver_web_app_button",
        lambda **kwargs: sent.append(kwargs),
    )

    launch_module._default_deliver(destination=None, url=TUNNEL_URL, label="x", title="y")

    assert sent and sent[0]["url"] == TUNNEL_URL


def test_a_launch_with_no_bot_token_rolls_everything_back(
    runtime, telegram_session, principal, experience_id, monkeypatch
):
    """The credential is absent, so the message cannot be sent — and is not faked."""
    from learning_studio.runtime.errors import LaunchRefused

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    with pytest.raises(LaunchRefused) as caught:
        launch_module.launch_experience(
            principal=principal,
            experience_id=experience_id,
            initiation="learner_request",
            learner_quote=LEARNER_QUOTE,
            config=config(),
        )

    assert caught.value.reason == "bot_token_absent"
    assert "do not tell them to tap anything" in caught.value.message.lower()
    assert len(runtime.store) == 0


# ── Destination ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("chat_type", ["dm", "private", "sender"])
def test_every_one_to_one_chat_type_resolves(
    runtime, telegram_session, principal, experience_id, monkeypatch, chat_type: str
):
    """`dm` is the one that actually arrives, and it used to be refused.

    Hermes normalises Telegram's `private` to its own canonical `dm` before the
    session variables are bound, so a plugin accepting only `private` refused
    every real direct message as though it were a group — the one surface the
    whole feature exists for.
    """
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", chat_type)
    deliver = Deliveries()

    result = launch(principal, experience_id, deliver=deliver)

    assert result["button_delivered"] is True
    assert deliver.sent[0]["chat_id"] == "1001"


@pytest.mark.parametrize("chat_type", ["group", "forum", "supergroup", "channel", "thread"])
def test_every_room_chat_type_is_refused(
    runtime, telegram_session, principal, experience_id, monkeypatch, chat_type: str
):
    from learning_studio.runtime.errors import LaunchRefused

    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", chat_type)
    deliver = Deliveries()

    with pytest.raises(LaunchRefused) as caught:
        launch(principal, experience_id, deliver=deliver)

    assert caught.value.reason == "destination_group_chat"
    assert deliver.sent == []


def test_an_unknown_chat_type_is_refused_rather_than_assumed(
    runtime, telegram_session, principal, experience_id, monkeypatch
):
    """The accepted set is an allowlist, so a future Hermes value fails closed."""
    from learning_studio.runtime.errors import LaunchRefused

    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "some_future_surface")

    with pytest.raises(LaunchRefused) as caught:
        launch(principal, experience_id, deliver=Deliveries())

    assert caught.value.reason == "destination_group_chat"


def test_a_group_conversation_is_refused(runtime, spoken, monkeypatch, principal, experience_id):
    from learning_studio.runtime.errors import LaunchRefused

    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "-100987")
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "supergroup")
    deliver = Deliveries()

    with pytest.raises(LaunchRefused) as caught:
        launch(principal, experience_id, deliver=deliver)

    assert caught.value.reason == "destination_group_chat"
    assert deliver.sent == []


def test_a_chat_that_is_not_the_sender_is_refused(
    runtime, spoken, monkeypatch, principal, experience_id
):
    """Belt and braces: this holds even when `chat_type` is absent."""
    from learning_studio.runtime.errors import LaunchRefused

    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "-100987")
    monkeypatch.delenv("HERMES_SESSION_CHAT_TYPE", raising=False)

    with pytest.raises(LaunchRefused) as caught:
        launch(principal, experience_id, deliver=Deliveries())

    assert caught.value.reason == "destination_not_the_sender"


def test_a_session_with_no_chat_is_refused(runtime, spoken, monkeypatch, principal, experience_id):
    from learning_studio.runtime.errors import LaunchRefused

    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)

    with pytest.raises(LaunchRefused) as caught:
        launch(principal, experience_id, deliver=Deliveries())

    assert caught.value.reason == "destination_absent"


def test_an_account_off_the_allowlist_is_refused(
    runtime, telegram_session, principal, experience_id
):
    from learning_studio.runtime.errors import LaunchRefused

    with pytest.raises(LaunchRefused) as caught:
        launch(
            principal,
            experience_id,
            deliver=Deliveries(),
            settings=config(mini_app_allowed_telegram_users=("9999",)),
        )

    assert caught.value.reason == "destination_not_allowed"


def test_a_non_telegram_session_has_no_destination(hermes_home):
    """Tested on the resolver directly: a launch refuses on ownership first.

    A local CLI principal owns none of the exercises a Telegram learner
    prepared, so `launch_experience` never reaches the destination step for one
    — which is the right order, and would make this assertion vacuous if it
    went through the whole transaction.
    """
    from learning_studio.destination import resolve_destination
    from learning_studio.runtime.errors import LaunchRefused

    local = Principal(profile="default", platform="local", user_id="", source="local_profile")

    with pytest.raises(LaunchRefused) as caught:
        resolve_destination(principal=local, config=config())

    assert caught.value.reason == "destination_not_telegram"


def test_the_destination_is_never_taken_from_a_payload():
    """There is no argument for it, so there is nothing to override."""
    from learning_studio.schemas import LAUNCH_SCHEMA

    source = Path(launch_module.__file__).read_text(encoding="utf-8")

    assert "chat_id" not in LAUNCH_SCHEMA["parameters"]["properties"]
    assert 'args["chat_id"]' not in source
    assert 'args.get("chat_id")' not in source


# ── The transaction, and what it is allowed to claim ──────────────────────


def test_a_commit_failure_after_delivery_is_reported_as_unknown(
    runtime, spoken, principal, experience_id, monkeypatch
):
    """The message went out and the launch could not be committed.

    Claiming success would tell the learner to tap something that may admit
    nobody; claiming failure would say nothing was sent while a message sits in
    their chat. The only honest answer is that it is not known.
    """
    deliver = Deliveries()
    monkeypatch.setattr(launch_module, "_activate", lambda handle, launch_id: False)

    with pytest.raises(RuntimeUnavailable) as caught:
        launch(principal, experience_id, deliver=deliver, evidence=spoken)

    assert caught.value.reason == "commit_indeterminate"
    assert "cannot tell" in caught.value.message
    assert len(deliver.sent) == 1


def test_a_rollback_failure_is_reported_rather_than_claiming_nothing_was_saved(
    runtime, spoken, principal, experience_id, monkeypatch
):
    monkeypatch.setattr(launch_module, "_revoke", lambda handle, launch_id: False)

    with pytest.raises(RuntimeUnavailable) as caught:
        launch(
            principal,
            experience_id,
            deliver=Deliveries(fails=RuntimeError("telegram is down")),
            evidence=spoken,
        )

    assert caught.value.reason == "rollback_indeterminate"
    assert "could not confirm" in caught.value.message


def test_a_grant_admits_nobody_until_the_button_has_been_delivered(
    runtime, spoken, principal, experience_id
):
    """The window between "a selector exists" and "somebody was told about it"."""
    captured: list[str] = []

    def deliver_then_fail(*, destination, url, label, title, launch_id):
        captured.append(launch_id)
        # At this instant the grant exists. It must admit nobody.
        assert runtime.store.admit(launch_id=launch_id, telegram_user_id="1001") is None
        raise RuntimeError("telegram is down")

    with pytest.raises(RuntimeError):
        launch(principal, experience_id, deliver=deliver_then_fail, evidence=spoken)

    assert captured, "the delivery seam was never reached"
    assert runtime.store.admit(launch_id=captured[0], telegram_user_id="1001") is None


def test_a_committed_launch_admits_its_learner(runtime, spoken, principal, experience_id):
    result = launch(principal, experience_id, deliver=Deliveries(), evidence=spoken)

    admitted = runtime.store.admit(launch_id=result["launch_id"], telegram_user_id="1001")

    assert admitted is not None
    assert admitted.activated is True


def test_a_failed_commit_keeps_the_message_claimed(
    runtime, spoken, principal, experience_id, monkeypatch
):
    """An indeterminate launch must not let a retry send a second button.

    The message went out. Whether the exercise works is unknown — but a retry
    on the same sentence would put a *second* button in the chat, which is the
    one thing an unknown state must not be allowed to cause. The claim is kept,
    so the agent has to go back to the learner.
    """
    deliver = Deliveries()
    monkeypatch.setattr(launch_module, "_activate", lambda handle, launch_id: False)

    with pytest.raises(RuntimeUnavailable):
        launch(principal, experience_id, deliver=deliver, evidence=spoken)

    monkeypatch.setattr(
        launch_module,
        "_activate",
        launch_module._activate.__wrapped__
        if hasattr(launch_module._activate, "__wrapped__")
        else _real_activate,
    )

    with pytest.raises(ConsentRequired) as caught:
        launch(principal, experience_id, deliver=deliver, evidence=spoken)

    assert caught.value.reason == "confirmation_already_used"
    assert len(deliver.sent) == 1, "a retry sent a second button"


def _real_activate(handle, launch_id):
    from learning_studio.runtime import ownership as _ownership

    reply = _ownership.call(handle.record, _ownership.GRANT_ACTIVATE_PATH, {"launch_id": launch_id})
    return bool(reply.get("activated"))


def test_a_failure_before_anything_is_sent_hands_the_message_back(
    runtime, spoken, principal, experience_id, clock
):
    """A tunnel that is not ready proves no button went out, so a retry may."""
    fake = runtime
    fake.tunnel_url = ""
    deliver = Deliveries()

    with pytest.raises(RuntimeUnavailable) as caught:
        launch(principal, experience_id, deliver=deliver, evidence=spoken)
    assert caught.value.reason == "tunnel_not_ready"

    fake.tunnel_url = TUNNEL_URL
    result = launch(principal, experience_id, deliver=deliver, evidence=spoken)

    assert result["button_delivered"] is True


def test_two_launches_racing_on_one_message_deliver_exactly_one_button(
    runtime, spoken, principal, experience_id, monkeypatch
):
    """The race the reservation exists to arbitrate.

    Both callers validate the *same* unspent message and both reach the point
    of claiming it. Before the reservation there was nothing between them: two
    grants were created, two buttons went out, and the loser of the eventual
    spend was discarded in silence — the learner saw two messages and the agent
    was told twice that one had been sent.

    The barrier is what makes this deterministic. Both threads finish consent
    before either claims, so the interleaving under test happens on every run
    rather than on an unlucky one.
    """
    import threading

    from learning_studio import consent as consent_module

    barrier = threading.Barrier(2, timeout=10)
    real_reserve = consent_module.reserve

    def synchronised(decision, *, store=None):
        barrier.wait()
        return real_reserve(decision, store=store)

    monkeypatch.setattr(consent_module, "reserve", synchronised)

    deliver = Deliveries()
    outcomes: list[object] = []

    def attempt() -> None:
        try:
            outcomes.append(launch(principal, experience_id, deliver=deliver, evidence=spoken))
        except BaseException as exc:  # noqa: BLE001 - the failure is the assertion
            outcomes.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive(), "a racing launch never finished"

    delivered = [item for item in outcomes if isinstance(item, dict)]
    refused = [item for item in outcomes if isinstance(item, ConsentRequired)]

    assert len(deliver.sent) == 1, "one message authorised two buttons"
    assert len(delivered) == 1
    assert delivered[0]["button_delivered"] is True
    assert len(refused) == 1
    assert refused[0].reason == "confirmation_already_used"
    assert len(runtime.store) == 1, "one message created two launches"
