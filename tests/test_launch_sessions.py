"""One launch, one live session — and what "one" has to survive.

Every test here is a way the previous design produced two answers to the
question "which session is this launch?", or lost a learner's place in
answering it.
"""

from __future__ import annotations

import threading

import pytest

from learning_studio.runtime import grants as grants_module
from learning_studio.sessions import SessionScope, SessionStore


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def sessions(clock) -> SessionStore:
    return SessionStore(ttl_seconds=1800, max_sessions=50, clock=clock)


@pytest.fixture
def store(clock) -> grants_module.GrantStore:
    return grants_module.GrantStore(profile="default", generation=1, clock=clock)


def scope(user: str = "1001", experience: str = "exp-1") -> SessionScope:
    return SessionScope(
        profile="default",
        telegram_user_id=user,
        learner_id="learner-" + user,
        experience_id=experience,
        track_id=None,
    )


def granted(store, *, user: str = "1001", experience: str = "exp-1"):
    """A grant that has been delivered — created, then activated.

    Creation alone leaves it *pending* and admitting nobody, which is what
    stops a send that failed from leaving a working entrance behind. Every test
    here is about what happens after the button actually arrived.
    """
    created = store.create(
        {"telegram_user_id": user, "learner_id": "learner-" + user, "experience_id": experience}
    )
    store.activate(created["launch_id"])
    return store.admit(launch_id=created["launch_id"], telegram_user_id=user)


def open_session(store, sessions, grant, *, user: str = "1001", experience: str = "exp-1"):
    return store.admit_session(
        grant, lambda: sessions.create(scope(user, experience), component_count=3)
    )


# ── Exactly one live session ──────────────────────────────────────────────


def test_a_second_open_replaces_the_first_rather_than_joining_it(store, sessions, clock):
    """Two live tokens for one launch is two answers to one question."""
    grant = granted(store)

    first_token, first = open_session(store, sessions, grant)
    second_token, second = open_session(store, sessions, grant)

    assert first_token != second_token
    assert grant.session is second
    assert first.expired(clock()) is True
    assert second.expired(clock()) is False


def test_the_replaced_token_stops_working_immediately(store, sessions, clock):
    grant = granted(store)
    first_token, _ = open_session(store, sessions, grant)

    open_session(store, sessions, grant)

    from learning_studio.sessions import SessionError

    with pytest.raises(SessionError):
        sessions.resolve(first_token, profile="default", telegram_user_id="1001")


def test_reopening_resumes_rather_than_restarts(store, sessions):
    """A webview reload must not discard what the learner has already done."""
    grant = granted(store)
    _, first = open_session(store, sessions, grant)
    first.position = 2
    first.answers = {"q-one": {"a": 1}, "q-two": {"a": 2}}
    first.revealed = {"q-one": "my recall"}

    _, second = open_session(store, sessions, grant)

    assert second.position == 2
    assert set(second.answers) == {"q-one", "q-two"}
    assert second.revealed == {"q-one": "my recall"}


def test_progress_never_regresses_across_a_reload(store, sessions):
    grant = granted(store)
    _, first = open_session(store, sessions, grant)
    first.position = 3
    first.completed_at = 1234.0

    _, second = open_session(store, sessions, grant)

    assert second.position == 3
    assert second.completed is True


def test_concurrent_opens_leave_exactly_one_live_session(store, sessions, clock):
    """A double tap, or a webview that retries. Only one may end up live."""
    grant = granted(store)
    tokens: list[str] = []
    start = threading.Barrier(8)

    def attempt() -> None:
        start.wait()
        token, _ = open_session(store, sessions, grant)
        tokens.append(token)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    live = [session for session in grant.sessions if not session.expired(clock())]
    assert len(tokens) == 8
    assert len(live) == 1
    assert live[0] is grant.session


# ── Revocation reaches every token ────────────────────────────────────────


def test_revoking_expires_every_session_the_launch_ever_minted(store, sessions, clock):
    """A pointer to the newest left every earlier token working."""
    grant = granted(store)
    open_session(store, sessions, grant)
    open_session(store, sessions, grant)
    open_session(store, sessions, grant)

    store.revoke(grant.launch_id)

    assert all(session.expired(clock()) for session in grant.sessions)
    assert grant.session is None


def test_a_revoked_launch_admits_nobody_afterwards(store, sessions):
    grant = granted(store)
    open_session(store, sessions, grant)

    store.revoke(grant.launch_id)

    assert store.admit(launch_id=grant.launch_id, telegram_user_id="1001") is None


# ── The two clocks are different clocks ───────────────────────────────────


def test_an_open_session_outlives_the_button_window(store, sessions, clock):
    """The invitation expiring must not throw out somebody mid-question."""
    grant = granted(store)
    open_session(store, sessions, grant)

    clock.advance(grants_module.DEFAULT_GRANT_TTL_SECONDS + 60)

    assert store.admit(launch_id=grant.launch_id, telegram_user_id="1001") is not None


def test_an_unopened_button_stops_working_when_its_window_closes(store, clock):
    grant = granted(store)

    clock.advance(grants_module.DEFAULT_GRANT_TTL_SECONDS + 1)

    assert store.admit(launch_id=grant.launch_id, telegram_user_id="1001") is None


def test_a_grant_that_lapsed_while_open_does_not_become_a_second_launch(store, sessions, clock):
    """Crossing the button TTL with a live session must not create ambiguity."""
    grant = granted(store)
    open_session(store, sessions, grant)
    clock.advance(grants_module.DEFAULT_GRANT_TTL_SECONDS + 60)

    repeat = store.create(
        {"telegram_user_id": "1001", "learner_id": "learner-1001", "experience_id": "exp-1"}
    )

    assert repeat["reused"] is True
    assert repeat["launch_id"] == grant.launch_id
    assert len(store) == 1


# ── Selection is a rule, not an accident of ordering ──────────────────────


def test_progress_selects_the_open_launch_not_the_first_inserted(store, sessions, clock):
    """Dictionary order is not a decision, and it used to be the deciding one."""
    stale = granted(store)
    clock.advance(grants_module.DEFAULT_GRANT_TTL_SECONDS + 1)
    current = granted(store)
    _, session = open_session(store, sessions, current)
    session.position = 2

    reported = store.progress({"telegram_user_id": "1001", "experience_id": "exp-1"})

    assert reported["launch_id"] == current.launch_id
    assert reported["launch_id"] != stale.launch_id
    assert reported["position"] == 2


# ── Capacity never detaches somebody who is working ───────────────────────


def test_capacity_refuses_rather_than_orphaning_an_active_session(clock, sessions):
    """Learner B arriving must not make learner A's progress unreportable."""
    store = grants_module.GrantStore(profile="default", generation=1, clock=clock, max_grants=1)
    busy = granted(store, user="1001")
    _, session = open_session(store, sessions, busy, user="1001")
    session.position = 2

    with pytest.raises(grants_module.GrantCapacityError):
        store.create(
            {"telegram_user_id": "2002", "learner_id": "learner-2002", "experience_id": "exp-9"}
        )

    assert store.admit(launch_id=busy.launch_id, telegram_user_id="1001") is not None
    assert store.progress({"telegram_user_id": "1001", "experience_id": "exp-1"})["position"] == 2


def test_capacity_first_reclaims_launches_nobody_is_using(clock, sessions):
    store = grants_module.GrantStore(profile="default", generation=1, clock=clock, max_grants=1)
    granted(store, user="1001")
    clock.advance(grants_module.DEFAULT_GRANT_TTL_SECONDS + 1)

    created = store.create(
        {"telegram_user_id": "2002", "learner_id": "learner-2002", "experience_id": "exp-9"}
    )

    assert created["created"] is True
    assert len(store) == 1
