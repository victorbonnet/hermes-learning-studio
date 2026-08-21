"""One launch, one live session — and what "one" has to survive.

Every test here is a way the previous design produced two answers to the
question "which session is this launch?", or lost a learner's place in
answering it.
"""

from __future__ import annotations

import threading

import pytest

from learning_studio.runtime import grants as grants_module
from learning_studio.sessions import (
    SessionError,
    SessionScope,
    SessionStep,
    SessionStore,
    SessionTransitionError,
)


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


def plan(count: int = 3, *, reveal: tuple[str, ...] = ()) -> tuple[SessionStep, ...]:
    """The ordered steps of a ``count``-component exercise: ``c-1`` … ``c-n``."""
    return tuple(
        SessionStep(component_id=f"c-{index}", reveal_required=f"c-{index}" in reveal)
        for index in range(1, count + 1)
    )


def open_session(
    store,
    sessions,
    grant,
    *,
    user: str = "1001",
    experience: str = "exp-1",
    steps: tuple[SessionStep, ...] | None = None,
):
    ordered = plan() if steps is None else steps
    return store.admit_session(
        grant, lambda: sessions.create(scope(user, experience), steps=ordered)
    )


def answer(session, count: int = 1, *, now: float = 1000.0) -> None:
    """Walk a session forward by ``count`` components, through its own interface.

    Assigning ``position`` and ``answers`` — which is what these tests used to
    do — asserts against a state no learner could actually have reached, and
    would keep passing if the session stopped enforcing how one is reached.
    """
    for _ in range(count):
        step = session.steps[session.snapshot().progress.position]
        session.record_answer(step.component_id, {"value": True}, now=now)


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
    steps = plan(3, reveal=("c-1",))
    _, first = open_session(store, sessions, grant, steps=steps)
    first.reveal("c-1", "my recall", lambda: "the back")
    first.record_answer("c-1", {"text": "my recall", "self_rating": "good"}, now=1000.0)
    first.record_answer("c-2", {"a": 2}, now=1000.0)

    _, second = open_session(store, sessions, grant, steps=steps)

    assert second.snapshot().progress.position == 2
    assert set(second.responses) == {"c-1", "c-2"}
    assert second.revealed_attempts == {"c-1": "my recall"}


def test_progress_never_regresses_across_a_reload(store, sessions):
    grant = granted(store)
    _, first = open_session(store, sessions, grant)
    answer(first, 3, now=1234.0)

    _, second = open_session(store, sessions, grant)

    resumed = second.snapshot()
    assert resumed.progress.position == 3
    assert resumed.progress.completed is True
    assert resumed.completed_at == 1234.0


def test_a_scored_session_carries_its_result_across_a_reload(store, sessions):
    """Completion without the result it produced is a session scored twice.

    The replacement inherits the completion timestamp, so it is finished as far
    as the summary route is concerned; leaving the cached result behind makes
    that route recompute and record a second durable attempt for one session.
    """
    grant = granted(store)
    _, first = open_session(store, sessions, grant)
    answer(first, 3, now=1234.0)
    first.score_once(lambda completion: {"attempt_id": "attempt-1"})

    _, second = open_session(store, sessions, grant)

    assert second.snapshot().scored is True
    assert second.score_once(lambda completion: pytest.fail("scored twice")) == {
        "attempt_id": "attempt-1"
    }


def test_capacity_eviction_resumes_scored_state_instead_of_starting_a_second_attempt(store, clock):
    sessions = SessionStore(ttl_seconds=1800, max_sessions=1, clock=clock)
    first_grant = granted(store, user="1001", experience="exp-1")
    first_token, first = open_session(store, sessions, first_grant)
    answer(first, 3, now=clock.now)
    first.score_once(lambda completion: {"attempt_id": "attempt-1"})

    second_grant = granted(store, user="2002", experience="exp-2")
    open_session(store, sessions, second_grant, user="2002", experience="exp-2")
    assert first.expired(clock.now), "capacity did not retire the old bearer token"

    replacement_token, replacement = open_session(store, sessions, first_grant)

    assert replacement_token != first_token
    # The whole projection, not just the counts: a replacement that carried the
    # position but not the completion it was reached by is a session finished
    # for one reader and unfinished for the next.
    assert replacement.snapshot() == first.snapshot()
    assert replacement.score_once(lambda completion: pytest.fail("scored twice")) == {
        "attempt_id": "attempt-1"
    }


def test_capacity_eviction_never_extends_the_original_session_deadline(store, clock):
    sessions = SessionStore(ttl_seconds=60, max_sessions=1, clock=clock)
    first_grant = granted(store, user="1001", experience="exp-1")
    first_token, first = open_session(store, sessions, first_grant)
    first.record_answer("c-1", {"value": True}, now=clock.now)

    other_grant = granted(store, user="1002", experience="exp-2")
    open_session(store, sessions, other_grant)
    clock.advance(61)

    replacement_token, replacement = open_session(store, sessions, first_grant)

    with pytest.raises(SessionError):
        sessions.resolve(first_token, profile="default", telegram_user_id="1001")
    assert replacement_token != first_token
    assert replacement.snapshot().progress.position == 0
    assert replacement.responses == {}


def test_capacity_retained_state_survives_the_shorter_launch_invitation(clock):
    store = grants_module.GrantStore(profile="default", generation=1, clock=clock, ttl_seconds=60)
    sessions = SessionStore(ttl_seconds=120, max_sessions=1, clock=clock)
    first_grant = granted(store, user="1001", experience="exp-1")
    assert first_grant is not None
    _, first = open_session(store, sessions, first_grant)
    first.record_answer("c-1", {"value": True}, now=clock.now)

    other_grant = granted(store, user="1002", experience="exp-2")
    assert other_grant is not None
    open_session(store, sessions, other_grant)
    clock.advance(61)

    admitted = store.admit(launch_id=first_grant.launch_id, telegram_user_id="1001")
    assert admitted is first_grant
    _, replacement = open_session(store, sessions, admitted)
    assert replacement.snapshot().progress.position == 1
    assert replacement.responses == {"c-1": {"value": True}}


def test_an_evicted_predecessor_cannot_score_after_its_state_was_transferred(store, clock):
    sessions = SessionStore(ttl_seconds=1800, max_sessions=1, clock=clock)
    first_grant = granted(store, user="1001", experience="exp-1")
    _, first = open_session(store, sessions, first_grant)
    answer(first, 3, now=clock.now)

    second_grant = granted(store, user="2002", experience="exp-2")
    open_session(store, sessions, second_grant, user="2002", experience="exp-2")
    _, replacement = open_session(store, sessions, first_grant)
    callbacks = []

    with pytest.raises(SessionTransitionError) as caught:
        first.score_once(lambda completion: callbacks.append("retired") or {"attempt_id": "old"})

    assert caught.value.reason == "session_retired"
    assert replacement.score_once(
        lambda completion: callbacks.append("replacement") or {"attempt_id": "attempt-1"}
    ) == {"attempt_id": "attempt-1"}
    assert callbacks == ["replacement"]


def test_an_unscored_session_resumes_without_inventing_a_result(store, sessions):
    grant = granted(store)
    _, first = open_session(store, sessions, grant)
    answer(first, 1)

    _, second = open_session(store, sessions, grant)

    assert second.snapshot().scored is False


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


def test_a_launch_is_described_from_one_read_of_its_session(store, sessions, clock):
    """State and position come from the same moment, or they can contradict.

    Asking the session "are you finished?" and then "how far are you?" is two
    reads of something that moves, and the pair could report a launch as still
    *opened* beside a position that had already reached the end.
    """
    grant = granted(store)
    _, session = open_session(store, sessions, grant, steps=plan(2))
    answer(session, 2)
    reads: list[int] = []
    snapshot = session.snapshot

    def counted():
        reads.append(1)
        return snapshot()

    session.snapshot = counted

    reported = store.progress({"telegram_user_id": "1001", "experience_id": "exp-1"})

    assert len(reads) == 1, "the launch description read the session more than once"
    assert reported["state"] == "completed"
    assert (reported["position"], reported["component_count"]) == (2, 2)
    assert (reported["answered"], reported["completed"]) == (2, True)
    # Primitives, not the session's own value objects: this payload crosses the
    # control plane as JSON.
    assert [type(reported[field]) for field in ("position", "component_count", "answered")] == [
        int,
        int,
        int,
    ]
    assert reported["completed"] is True and reported["scored"] is False


def test_progress_selects_the_open_launch_not_the_first_inserted(store, sessions, clock):
    """Dictionary order is not a decision, and it used to be the deciding one."""
    stale = granted(store)
    clock.advance(grants_module.DEFAULT_GRANT_TTL_SECONDS + 1)
    current = granted(store)
    _, session = open_session(store, sessions, current)
    answer(session, 2)

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
    answer(session, 2)

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


def test_a_pending_grant_that_was_never_resolved_does_not_leak(clock):
    """A launch killed between create and commit must not hold capacity forever.

    A pending grant is kept while its transaction is plausibly still running —
    otherwise a crash mid-launch would leave one nothing can resolve, counting
    against the runtime's capacity until it restarts.
    """
    store = grants_module.GrantStore(profile="default", generation=1, clock=clock)
    store.create(
        {"telegram_user_id": "1001", "learner_id": "learner-1001", "experience_id": "exp-1"}
    )
    assert len(store) == 1

    clock.advance(grants_module.DEFAULT_GRANT_TTL_SECONDS + 1)
    store.purge()

    assert len(store) == 0


def test_a_pending_grant_is_kept_while_its_launch_is_still_running(clock):
    store = grants_module.GrantStore(profile="default", generation=1, clock=clock)
    store.create(
        {"telegram_user_id": "1001", "learner_id": "learner-1001", "experience_id": "exp-1"}
    )

    clock.advance(1)
    store.purge()

    assert len(store) == 1


def test_a_pending_grant_admits_nobody(clock):
    """The window between "a selector exists" and "somebody was told about it"."""
    store = grants_module.GrantStore(profile="default", generation=1, clock=clock)
    created = store.create(
        {"telegram_user_id": "1001", "learner_id": "learner-1001", "experience_id": "exp-1"}
    )

    assert store.admit(launch_id=created["launch_id"], telegram_user_id="1001") is None
    assert created["state"] == "pending"


# ── Revocation cannot be lost in the admission window ─────────────────────


def test_a_revocation_between_admit_and_admit_session_is_not_lost(store, sessions):
    """The window the request path actually has, and what used to fall in it.

    ``admit`` hands the grant back and releases the lock; the request then does
    an ownership query and loads a bundle before asking for a session. A
    revocation landing in that gap used to be silently undone — the grant was
    popped and its sessions retired, and then ``admit_session`` attached a new
    live one to the object still in the caller's hand. The result was a working
    token for a launch the store no longer had, which nothing could revoke
    afterwards because nothing could find it.
    """
    grant = granted(store)

    assert store.revoke(grant.launch_id) is True

    assert open_session(store, sessions, grant) is None
    assert grant.session is None
    assert len(sessions) == 0, "a revoked launch minted a session"


def test_a_grant_that_expired_in_the_admission_window_mints_nothing(store, sessions, clock):
    """Same window, different cause. Expiry is checked again, not assumed."""
    grant = granted(store)

    clock.advance(grants_module.DEFAULT_GRANT_TTL_SECONDS + 1)

    assert open_session(store, sessions, grant) is None
    assert len(sessions) == 0


def test_a_concurrent_revocation_and_admission_never_both_win(store, sessions):
    """Under real contention, with the interleaving forced rather than hoped for."""
    import threading

    grant = granted(store)
    barrier = threading.Barrier(2, timeout=10)
    outcomes: dict[str, object] = {}

    def revoking() -> None:
        barrier.wait()
        outcomes["revoked"] = store.revoke(grant.launch_id)

    def admitting() -> None:
        barrier.wait()
        outcomes["admitted"] = open_session(store, sessions, grant)

    threads = [threading.Thread(target=revoking), threading.Thread(target=admitting)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()

    assert outcomes["revoked"] is True
    # Whichever order the lock granted, a revoked launch holds no live session.
    assert store.admit(launch_id=grant.launch_id, telegram_user_id="1001") is None
    assert grant.session is None or grant.session.expires_at <= 0


# ── Two expiries that both ran out must not add up to no expiry ───────────


def test_a_grant_whose_window_closed_cannot_be_activated(store, clock):
    """A button that arrives after its own window is not a launch.

    Activating anyway produced the worst pair of facts available: a launch
    reported open, and an entrance that admits nobody. The caller now learns it
    could not be committed and rolls back.
    """
    created = store.create(
        {"telegram_user_id": "1001", "learner_id": "learner-1001", "experience_id": "exp-1"}
    )

    clock.advance(grants_module.DEFAULT_GRANT_TTL_SECONDS + 1)

    assert store.activate(created["launch_id"]) is False
    assert store.admit(launch_id=created["launch_id"], telegram_user_id="1001") is None


def test_an_expired_session_does_not_keep_a_grant_alive_for_ever(store, sessions, clock):
    """A pointer to a session is not a session.

    Retirement sets an expiry; it does not clear the field. So "has a session?"
    answered yes for ever, and a launch that had been opened once stayed
    admissible past its own window *and* past the session's — long enough to
    mint a brand new one.
    """
    grant = granted(store)
    open_session(store, sessions, grant)

    clock.advance(grants_module.DEFAULT_GRANT_TTL_SECONDS + 1)
    clock.advance(sessions._ttl + 1)

    assert grant.session is not None, "the pointer is still there; that is the point"
    assert grant.admissible(clock()) is False
    assert store.admit(launch_id=grant.launch_id, telegram_user_id="1001") is None
    assert open_session(store, sessions, grant) is None


def test_progress_stops_being_reported_when_the_session_runs_out(store, sessions, clock):
    """A learner's position must not outlive the thing that bounds it."""
    grant = granted(store)
    # Five steps, three answered: far enough in to have a position worth
    # reporting, and not so far that the launch reads as completed instead.
    open_session(store, sessions, grant, steps=plan(5))
    answer(grant.session, 3)

    live = store.progress({"telegram_user_id": "1001", "experience_id": "exp-1"})
    assert live["position"] == 3
    assert live["state"] == "opened"

    clock.advance(sessions._ttl + 1)

    after = store.progress({"telegram_user_id": "1001", "experience_id": "exp-1"})
    assert after["position"] == 0
    assert after["answered"] == 0
    assert after["state"] == "closed"


def test_expired_session_state_is_not_carried_into_a_fresh_session(clock):
    store = grants_module.GrantStore(profile="default", generation=7, clock=clock, ttl_seconds=600)
    sessions = SessionStore(clock=clock, ttl_seconds=60, max_sessions=50)
    grant = granted(store)
    _, old = open_session(store, sessions, grant)
    answer(old, 2)

    clock.advance(61)
    assert store.admit(launch_id=grant.launch_id, telegram_user_id="1001") is grant
    opened = open_session(store, sessions, grant)
    assert opened is not None
    _, fresh = opened
    assert fresh.snapshot().progress.position == 0
    assert fresh.responses == {}
