"""Mini App session scope, expiry, and opacity."""

from __future__ import annotations

import pytest

from learning_studio.sessions import (
    MiniAppSession,
    SessionError,
    SessionScope,
    SessionStore,
)


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def scope(user: str = "1001", experience: str = "exp-1", profile: str = "default") -> SessionScope:
    return SessionScope(
        profile=profile,
        telegram_user_id=user,
        learner_id="learner-" + user,
        experience_id=experience,
        track_id=None,
    )


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock: Clock) -> SessionStore:
    return SessionStore(ttl_seconds=600, max_sessions=10, clock=clock)


def test_a_created_session_resolves_for_its_own_owner(store: SessionStore):
    token, session = store.create(scope(), component_count=3)

    resolved = store.resolve(token, profile="default", telegram_user_id="1001")

    assert resolved is session
    assert resolved.scope.experience_id == "exp-1"


def test_tokens_are_opaque_and_unique(store: SessionStore):
    first, _ = store.create(scope(), component_count=1)
    second, _ = store.create(scope(experience="exp-2"), component_count=1)

    assert first != second
    for token in (first, second):
        assert "1001" not in token
        assert "exp-" not in token
        assert "default" not in token
    assert len(first) >= 32


def test_the_raw_token_is_not_retained_by_the_store(store: SessionStore):
    token, session = store.create(scope(), component_count=1)

    assert token not in repr(store.__dict__)
    assert token not in repr(session)
    assert token not in session.ref


# ── Scope is checked on every use ─────────────────────────────────────────


def test_another_telegram_account_cannot_use_the_token(store: SessionStore):
    """A leaked token is inert without the identity it was minted for."""
    token, _ = store.create(scope(user="1001"), component_count=1)

    with pytest.raises(SessionError):
        store.resolve(token, profile="default", telegram_user_id="2002")


def test_another_profile_cannot_use_the_token(store: SessionStore):
    token, _ = store.create(scope(), component_count=1)

    with pytest.raises(SessionError):
        store.resolve(token, profile="work", telegram_user_id="1001")


def test_an_unknown_token_is_refused(store: SessionStore):
    store.create(scope(), component_count=1)

    with pytest.raises(SessionError):
        store.resolve("not-a-real-token", profile="default", telegram_user_id="1001")


@pytest.mark.parametrize("token", [None, "", "   ", 12345, b"bytes"])
def test_a_malformed_token_is_refused(store: SessionStore, token):
    with pytest.raises(SessionError):
        store.resolve(token, profile="default", telegram_user_id="1001")


def test_every_refusal_says_the_same_thing(store: SessionStore):
    """Absent, expired, and not-yours must be indistinguishable to a caller."""
    token, _ = store.create(scope(), component_count=1)
    messages = set()

    for call in (
        lambda: store.resolve("unknown", profile="default", telegram_user_id="1001"),
        lambda: store.resolve(token, profile="default", telegram_user_id="2002"),
        lambda: store.resolve(token, profile="other", telegram_user_id="1001"),
    ):
        with pytest.raises(SessionError) as caught:
            call()
        messages.add(str(caught.value))

    assert len(messages) == 1


# ── Expiry ────────────────────────────────────────────────────────────────


def test_a_session_expires(store: SessionStore, clock: Clock):
    token, _ = store.create(scope(), component_count=1)
    clock.advance(601)

    with pytest.raises(SessionError):
        store.resolve(token, profile="default", telegram_user_id="1001")


def test_a_session_is_usable_right_up_to_expiry(store: SessionStore, clock: Clock):
    token, _ = store.create(scope(), component_count=1)
    clock.advance(599)

    assert store.resolve(token, profile="default", telegram_user_id="1001")


def test_an_expired_session_is_dropped_not_merely_refused(store: SessionStore, clock: Clock):
    token, _ = store.create(scope(), component_count=1)
    clock.advance(601)

    with pytest.raises(SessionError):
        store.resolve(token, profile="default", telegram_user_id="1001")
    assert len(store) == 0


def test_expired_sessions_are_swept(store: SessionStore, clock: Clock):
    store.create(scope(experience="exp-1"), component_count=1)
    store.create(scope(experience="exp-2"), component_count=1)
    clock.advance(601)

    assert store.purge_expired() == 2
    assert len(store) == 0


# ── Bounded memory ────────────────────────────────────────────────────────


def test_the_store_stays_bounded(clock: Clock):
    store = SessionStore(ttl_seconds=600, max_sessions=3, clock=clock)

    for index in range(20):
        clock.advance(1)
        store.create(scope(experience=f"exp-{index}"), component_count=1)

    assert len(store) <= 3


def test_dropping_a_session_ends_it(store: SessionStore):
    token, _ = store.create(scope(), component_count=1)
    store.drop(token)

    with pytest.raises(SessionError):
        store.resolve(token, profile="default", telegram_user_id="1001")


# ── Progress state ────────────────────────────────────────────────────────


def test_a_new_session_starts_at_the_first_component(store: SessionStore):
    _, session = store.create(scope(), component_count=4)

    assert (session.position, session.completed, session.answers) == (0, False, {})


def test_completion_is_recorded_on_the_session(store: SessionStore, clock: Clock):
    _, session = store.create(scope(), component_count=1)
    session.completed_at = clock.now

    assert isinstance(session, MiniAppSession)
    assert session.completed


# ── Authenticated activity, which is what the runtime's idle timer reads ──


def test_a_new_store_reports_no_activity(clock):
    store = SessionStore(ttl_seconds=100, max_sessions=10, clock=clock)

    assert store.last_activity_at is None


def test_activity_is_recorded_against_the_store_clock(clock):
    store = SessionStore(ttl_seconds=100, max_sessions=10, clock=clock)
    clock.advance(50)

    store.note_activity()

    assert store.last_activity_at == clock.now


def test_activity_outlives_the_session_that_produced_it(clock):
    """A learner working for an hour holds several sessions in succession.

    The idle timer must read that as continuous use, so the timestamp lives on
    the store rather than on any one session.
    """
    store = SessionStore(ttl_seconds=10, max_sessions=10, clock=clock)
    store.create(scope(), component_count=1)
    store.note_activity()
    recorded = store.last_activity_at

    clock.advance(11)
    store.purge_expired()

    assert len(store) == 0
    assert store.last_activity_at == recorded


def test_creating_a_session_does_not_by_itself_count_as_activity(clock):
    """The API records activity explicitly, at the two authenticated points.

    Making creation implicitly count would mean a store used by something other
    than the API silently drove a runtime's idle timer.
    """
    store = SessionStore(ttl_seconds=100, max_sessions=10, clock=clock)

    store.create(scope(), component_count=1)

    assert store.last_activity_at is None
