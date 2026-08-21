"""Mini App session scope, expiry, opacity — and the lifecycle it owns."""

from __future__ import annotations

import threading
from dataclasses import FrozenInstanceError

import pytest

from learning_studio.sessions import (
    SessionError,
    SessionProgress,
    SessionScope,
    SessionStep,
    SessionStore,
    SessionTransitionError,
    SessionView,
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


def plan(count: int = 1, *, reveal: tuple[str, ...] = ()) -> tuple[SessionStep, ...]:
    """The ordered steps of a ``count``-component exercise: ``c-1`` … ``c-n``.

    ``reveal`` names the steps whose card must be turned over first, which is
    the only thing a step knows about a component beyond its identifier.
    """
    return tuple(
        SessionStep(component_id=f"c-{index}", reveal_required=f"c-{index}" in reveal)
        for index in range(1, count + 1)
    )


def test_capacity_eviction_retires_the_session_object():
    clock = Clock()
    store = SessionStore(clock=clock, ttl_seconds=60, max_sessions=1)
    _, first = store.create(scope(), steps=plan(1))
    store.create(scope(experience="exp-2"), steps=plan(1))

    assert first.expired(clock())


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock: Clock) -> SessionStore:
    return SessionStore(ttl_seconds=600, max_sessions=10, clock=clock)


def test_a_created_session_resolves_for_its_own_owner(store: SessionStore):
    token, session = store.create(scope(), steps=plan(3))

    resolved = store.resolve(token, profile="default", telegram_user_id="1001")

    assert resolved is session
    assert resolved.scope.experience_id == "exp-1"


def test_tokens_are_opaque_and_unique(store: SessionStore):
    first, _ = store.create(scope(), steps=plan(1))
    second, _ = store.create(scope(experience="exp-2"), steps=plan(1))

    assert first != second
    for token in (first, second):
        assert "1001" not in token
        assert "exp-" not in token
        assert "default" not in token
    assert len(first) >= 32


def test_the_raw_token_is_not_retained_by_the_store(store: SessionStore):
    token, session = store.create(scope(), steps=plan(1))

    assert token not in repr(store.__dict__)
    assert token not in repr(session)
    assert token not in session.ref


# ── Scope is checked on every use ─────────────────────────────────────────


def test_another_telegram_account_cannot_use_the_token(store: SessionStore):
    """A leaked token is inert without the identity it was minted for."""
    token, _ = store.create(scope(user="1001"), steps=plan(1))

    with pytest.raises(SessionError):
        store.resolve(token, profile="default", telegram_user_id="2002")


def test_another_profile_cannot_use_the_token(store: SessionStore):
    token, _ = store.create(scope(), steps=plan(1))

    with pytest.raises(SessionError):
        store.resolve(token, profile="work", telegram_user_id="1001")


def test_an_unknown_token_is_refused(store: SessionStore):
    store.create(scope(), steps=plan(1))

    with pytest.raises(SessionError):
        store.resolve("not-a-real-token", profile="default", telegram_user_id="1001")


@pytest.mark.parametrize("token", [None, "", "   ", 12345, b"bytes"])
def test_a_malformed_token_is_refused(store: SessionStore, token):
    with pytest.raises(SessionError):
        store.resolve(token, profile="default", telegram_user_id="1001")


def test_every_refusal_says_the_same_thing(store: SessionStore):
    """Absent, expired, and not-yours must be indistinguishable to a caller."""
    token, _ = store.create(scope(), steps=plan(1))
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
    token, _ = store.create(scope(), steps=plan(1))
    clock.advance(601)

    with pytest.raises(SessionError):
        store.resolve(token, profile="default", telegram_user_id="1001")


def test_a_session_is_usable_right_up_to_expiry(store: SessionStore, clock: Clock):
    token, _ = store.create(scope(), steps=plan(1))
    clock.advance(599)

    assert store.resolve(token, profile="default", telegram_user_id="1001")


def test_an_expired_session_is_dropped_not_merely_refused(store: SessionStore, clock: Clock):
    token, _ = store.create(scope(), steps=plan(1))
    clock.advance(601)

    with pytest.raises(SessionError):
        store.resolve(token, profile="default", telegram_user_id="1001")
    assert len(store) == 0


def test_expired_sessions_are_swept(store: SessionStore, clock: Clock):
    store.create(scope(experience="exp-1"), steps=plan(1))
    store.create(scope(experience="exp-2"), steps=plan(1))
    clock.advance(601)

    assert store.purge_expired() == 2
    assert len(store) == 0


# ── Bounded memory ────────────────────────────────────────────────────────


def test_the_store_stays_bounded(clock: Clock):
    store = SessionStore(ttl_seconds=600, max_sessions=3, clock=clock)

    for index in range(20):
        clock.advance(1)
        store.create(scope(experience=f"exp-{index}"), steps=plan(1))

    assert len(store) <= 3


def test_dropping_a_session_ends_it(store: SessionStore):
    token, session = store.create(scope(), steps=plan(1))
    store.drop(token)

    with pytest.raises(SessionError):
        store.resolve(token, profile="default", telegram_user_id="1001")
    with pytest.raises(SessionTransitionError) as caught:
        session.record_answer("c-1", {"value": True}, now=1000.0)
    assert caught.value.reason == "session_retired"


# ── Progression is the session's own business ─────────────────────────────
#
# Every test below drives the session interface rather than assigning to its
# state. That is the point of the interface: a caller that could set the
# position, the answers, or the completion time could also set an inconsistent
# combination of them, and the invariants would live in whichever caller
# happened to be careful.


def lifecycle(session) -> dict:
    """Everything a refused transition must leave exactly as it was.

    The session's own projection — which is one frozen value, so comparing two
    of them compares every lifecycle field at once — beside the two copies that
    are deliberately *not* in it: what the learner wrote, and the recall they
    were held to when a card was turned over.
    """
    return {
        "view": session.snapshot(),
        "responses": session.responses,
        "revealed": session.revealed_attempts,
    }


# ── One coherent read of the whole lifecycle ──────────────────────────────


def test_a_snapshot_reports_every_lifecycle_field_from_one_state(store: SessionStore, clock: Clock):
    """One projection, taken once, whose fields cannot disagree with each other.

    Position, the component that position names, and the answers behind it are
    read together: a consumer that asked for them separately could be handed a
    position from before another request's transition and a component from
    after it.
    """
    _, session = store.create(scope(), steps=plan(3))
    session.record_answer("c-1", {"value": True}, now=clock.now)

    view = session.snapshot()

    assert (view.progress.position, view.progress.component_count) == (1, 3)
    assert (view.progress.answered, view.progress.completed) == (1, False)
    assert view.current_component_id == "c-2"
    assert view.answered_component_ids == ("c-1",)
    assert (view.completed_at, view.scored) == (None, False)
    # A value, not a handle on the session.
    with pytest.raises(FrozenInstanceError):
        view.progress.position = 99
    with pytest.raises(FrozenInstanceError):
        view.scored = True


def test_a_snapshot_does_not_move_when_the_session_does(store: SessionStore, clock: Clock):
    """What a reader was told stays what they were told.

    A projection that tracked the session would be the original defect wearing
    a different name: the answer a request already decided on would change
    under it while the response was still being built.
    """
    _, session = store.create(scope(), steps=plan(2))
    before = session.snapshot()

    session.record_answer("c-1", {"value": True}, now=clock.now)
    session.record_answer("c-2", {"value": False}, now=clock.now)

    assert before.progress == SessionProgress(
        position=0, component_count=2, answered=0, completed=False
    )
    assert (before.current_component_id, before.answered_component_ids) == ("c-1", ())
    assert (before.completed_at, before.scored) == (None, False)
    assert session.snapshot() != before


def test_a_receipt_is_frozen_and_describes_only_its_own_move(store: SessionStore, clock: Clock):
    _, session = store.create(scope(), steps=plan(3))

    receipt = session.record_answer("c-1", {"value": True}, now=clock.now)
    session.record_answer("c-2", {"value": True}, now=clock.now)

    # The second answer moved the session on; the first answer's receipt still
    # says what the first answer did.
    assert receipt.next_component_id == "c-2"
    assert receipt.progress.position == 1
    with pytest.raises(FrozenInstanceError):
        receipt.next_component_id = "c-3"
    with pytest.raises(FrozenInstanceError):
        receipt.progress.answered = 99


def test_no_projection_carries_a_learner_value_into_a_repr(store: SessionStore, clock: Clock):
    """These values travel to adapters, grants, and logs. Nothing private rides.

    A repr is where a private value escapes without anybody deciding to send
    it: a debugger, an exception formatter, a log line that interpolates the
    object it was handed.
    """
    _, session = store.create(scope(), steps=plan(2, reveal=("c-1",)))
    session.reveal("c-1", "RAW-RECALL-CANARY", lambda: "HIDDEN-BACK-CANARY")
    receipt = session.record_answer(
        "c-1", {"text": "RAW-RECALL-CANARY", "self_rating": "good"}, now=clock.now
    )
    answered = session.record_answer("c-2", {"text": "RAW-RESPONSE-CANARY"}, now=clock.now)
    session.score_once(lambda completion: {"attempt_id": "ATTEMPT-CANARY", "overall_score": 1})

    rendered = " ".join(
        repr(value)
        for value in (
            session.snapshot(),
            session.snapshot().progress,
            receipt,
            answered,
        )
    )

    for canary in (
        "RAW-RECALL-CANARY",
        "HIDDEN-BACK-CANARY",
        "RAW-RESPONSE-CANARY",
        "ATTEMPT-CANARY",
    ):
        assert canary not in rendered


def test_a_new_session_starts_at_the_first_component(store: SessionStore):
    _, session = store.create(scope(), steps=plan(4))
    view = session.snapshot()

    assert view.progress == SessionProgress(
        position=0, component_count=4, answered=0, completed=False
    )
    assert view.current_component_id == "c-1"
    assert (view.answered_component_ids, session.responses) == ((), {})
    assert (view.completed_at, view.scored) == (None, False)


def test_recording_the_current_answer_advances_exactly_one_step(store: SessionStore, clock: Clock):
    _, session = store.create(scope(), steps=plan(3))

    receipt = session.record_answer("c-1", {"value": True}, now=clock.now)

    assert receipt.progress == SessionProgress(
        position=1, component_count=3, answered=1, completed=False
    )
    assert receipt.next_component_id == "c-2"
    assert session.responses == {"c-1": {"value": True}}
    assert session.snapshot().completed_at is None


def test_a_caller_cannot_mutate_a_recorded_response(store: SessionStore, clock: Clock):
    _, session = store.create(scope(), steps=plan(2))
    submitted = {"nested": {"value": True}}

    session.record_answer("c-1", submitted, now=clock.now)
    submitted["nested"]["value"] = False

    assert session.responses == {"c-1": {"nested": {"value": True}}}


def test_completion_is_recorded_only_on_the_final_component(store: SessionStore, clock: Clock):
    _, session = store.create(scope(), steps=plan(2))
    session.record_answer("c-1", {"value": True}, now=clock.now)
    assert session.snapshot().progress.completed is False

    clock.advance(30)
    receipt = session.record_answer("c-2", {"value": False}, now=clock.now)

    assert receipt.progress.completed is True
    # There is no card after the last one, and the receipt says so rather than
    # leaving the caller to work it out from a position it would have to read.
    assert receipt.next_component_id is None
    finished = session.snapshot()
    assert (finished.progress.completed, finished.completed_at) == (True, clock.now)
    assert finished.answered_component_ids == ("c-1", "c-2")
    assert finished.current_component_id is None


# ── A refused transition changes nothing ──────────────────────────────────


def test_answering_a_component_the_session_has_left_behind_is_refused(
    store: SessionStore, clock: Clock
):
    """A stale client must not be able to answer the previous question again."""
    _, session = store.create(scope(), steps=plan(3))
    session.record_answer("c-1", {"value": True}, now=clock.now)
    before = lifecycle(session)

    with pytest.raises(SessionTransitionError) as caught:
        session.record_answer("c-1", {"value": False}, now=clock.now)

    assert caught.value.reason == "component_mismatch"
    assert lifecycle(session) == before


@pytest.mark.parametrize("claimed", ["c-3", "no-such-card", "", None, 7, {"c-2": 1}])
def test_a_component_that_is_not_the_current_one_is_refused(
    store: SessionStore, clock: Clock, claimed
):
    """Naming a later card would walk the exercise out of order."""
    _, session = store.create(scope(), steps=plan(3))
    session.record_answer("c-1", {"value": True}, now=clock.now)
    before = lifecycle(session)

    with pytest.raises(SessionTransitionError) as caught:
        session.record_answer(claimed, {"value": True}, now=clock.now)

    assert caught.value.reason == "component_mismatch"
    assert lifecycle(session) == before


def test_a_finished_session_records_nothing_more(store: SessionStore, clock: Clock):
    _, session = store.create(scope(), steps=plan(1))
    session.record_answer("c-1", {"value": True}, now=clock.now)
    before = lifecycle(session)

    clock.advance(60)
    with pytest.raises(SessionTransitionError) as caught:
        session.record_answer("c-1", {"value": False}, now=clock.now)

    assert caught.value.reason == "already_complete"
    # The completion time in particular: a second submission must not restamp
    # when the learner finished.
    assert lifecycle(session) == before


def test_a_session_over_no_components_has_nothing_to_record(store: SessionStore, clock: Clock):
    """Position past the end without a completion is its own refusal."""
    _, session = store.create(scope(), steps=())

    with pytest.raises(SessionTransitionError) as caught:
        session.record_answer("c-1", {"value": True}, now=clock.now)

    assert caught.value.reason == "no_component"
    assert lifecycle(session) == {
        "view": SessionView(
            progress=SessionProgress(position=0, component_count=0, answered=0, completed=False),
            current_component_id=None,
            answered_component_ids=(),
            completed_at=None,
            scored=False,
        ),
        "responses": {},
        "revealed": {},
    }


def test_the_ordered_plan_cannot_be_replaced_after_creation(store: SessionStore):
    _, session = store.create(scope(), steps=plan(2))

    with pytest.raises(AttributeError):
        session.steps = (SessionStep("injected"),)

    assert session.steps == plan(2)


def test_duplicate_component_ids_are_not_a_valid_plan(store: SessionStore):
    with pytest.raises(ValueError, match="duplicate component_id"):
        store.create(scope(), steps=(SessionStep("same"), SessionStep("same")))


# ── Turning a card over ───────────────────────────────────────────────────


def test_the_reveal_discloses_before_it_freezes_the_attempt(store: SessionStore):
    """The order is the security property: the disclosure has to succeed first."""
    _, session = store.create(scope(), steps=plan(2, reveal=("c-1",)))
    frozen_when_asked = []

    def disclose() -> str:
        frozen_when_asked.append(session.revealed_attempts)
        return "the back"

    outcome = session.reveal("c-1", "my recall", disclose)

    assert frozen_when_asked == [{}], "the attempt was frozen before the reveal succeeded"
    assert (outcome.back, outcome.attempt) == ("the back", "my recall")
    assert session.revealed_attempts == {"c-1": "my recall"}
    # Turning a card over is not answering it.
    assert session.snapshot().progress.position == 0


def test_a_reveal_outcome_repr_contains_neither_hidden_answer_nor_raw_recall(store: SessionStore):
    _, session = store.create(scope(), steps=plan(1, reveal=("c-1",)))

    outcome = session.reveal("c-1", "RAW-RECALL-CANARY", lambda: "HIDDEN-BACK-CANARY")

    rendered = repr(outcome)
    assert "RAW-RECALL-CANARY" not in rendered
    assert "HIDDEN-BACK-CANARY" not in rendered


def test_a_failed_disclosure_leaves_no_frozen_attempt(store: SessionStore):
    """Otherwise the learner is held to a recall they never got an answer for."""
    _, session = store.create(scope(), steps=plan(2, reveal=("c-1",)))
    before = lifecycle(session)

    def unavailable() -> str:
        raise RuntimeError("the store was unavailable")

    with pytest.raises(RuntimeError):
        session.reveal("c-1", "attempt A", unavailable)

    assert lifecycle(session) == before
    # So the next attempt is the one that counts.
    assert session.reveal("c-1", "attempt B", lambda: "the back").attempt == "attempt B"


def test_a_repeated_reveal_keeps_the_first_attempt(store: SessionStore):
    """A refresh must be safe, and must not be a second chance."""
    _, session = store.create(scope(), steps=plan(2, reveal=("c-1",)))
    session.reveal("c-1", "my first guess", lambda: "the back")

    second = session.reveal("c-1", "the answer, copied", lambda: "the back")

    assert second.attempt == "my first guess"
    assert session.revealed_attempts == {"c-1": "my first guess"}


def test_a_card_with_nothing_to_turn_over_is_refused(store: SessionStore):
    """Eligibility is read from the step, so the disclosure is never attempted."""
    _, session = store.create(scope(), steps=plan(1))
    before = lifecycle(session)

    with pytest.raises(SessionTransitionError) as caught:
        session.reveal("c-1", "peeking", lambda: pytest.fail("the disclosure was called"))

    assert caught.value.reason == "type_not_revealable"
    assert lifecycle(session) == before


def test_a_card_the_session_is_not_on_cannot_be_revealed(store: SessionStore):
    _, session = store.create(scope(), steps=plan(2, reveal=("c-1", "c-2")))

    with pytest.raises(SessionTransitionError) as caught:
        session.reveal("c-2", "peeking", lambda: pytest.fail("the disclosure was called"))

    assert caught.value.reason == "component_mismatch"
    assert session.revealed_attempts == {}


def test_a_finished_session_cannot_reveal(store: SessionStore, clock: Clock):
    _, session = store.create(scope(), steps=plan(1, reveal=("c-1",)))
    session.reveal("c-1", "yama", lambda: "the back")
    session.record_answer("c-1", {"text": "yama", "self_rating": "good"}, now=clock.now)
    before = lifecycle(session)

    with pytest.raises(SessionTransitionError) as caught:
        session.reveal("c-1", "yama", lambda: pytest.fail("the disclosure was called"))

    assert caught.value.reason == "already_complete"
    assert lifecycle(session) == before


# ── The reveal contract, enforced on the submission ───────────────────────


def test_a_card_that_must_be_turned_over_cannot_be_answered_first(
    store: SessionStore, clock: Clock
):
    """Self-rating a recall that was never committed is not retrieval practice."""
    _, session = store.create(scope(), steps=plan(2, reveal=("c-1",)))
    before = lifecycle(session)

    with pytest.raises(SessionTransitionError) as caught:
        session.record_answer("c-1", {"text": "yama", "self_rating": "good"}, now=clock.now)

    assert caught.value.reason == "reveal_required"
    assert lifecycle(session) == before


def test_the_recall_cannot_be_rewritten_after_the_answer_was_read(
    store: SessionStore, clock: Clock
):
    """The attack this exists to stop: peek, then submit the answer as recall."""
    _, session = store.create(scope(), steps=plan(2, reveal=("c-1",)))
    session.reveal("c-1", "no idea", lambda: "the back")
    before = lifecycle(session)

    with pytest.raises(SessionTransitionError) as caught:
        session.record_answer("c-1", {"text": "the back", "self_rating": "easy"}, now=clock.now)

    assert caught.value.reason == "recall_changed_after_reveal"
    assert lifecycle(session) == before


def test_the_frozen_recall_is_what_may_be_recorded(store: SessionStore, clock: Clock):
    _, session = store.create(scope(), steps=plan(2, reveal=("c-1",)))
    session.reveal("c-1", "no idea", lambda: "the back")

    receipt = session.record_answer(
        "c-1", {"text": "no idea", "self_rating": "again"}, now=clock.now
    )

    assert receipt.progress == SessionProgress(
        position=1, component_count=2, answered=1, completed=False
    )


def test_a_card_with_nothing_to_turn_over_needs_no_reveal(store: SessionStore, clock: Clock):
    """The contract applies to revealable steps and to no others."""
    _, session = store.create(scope(), steps=plan(1))

    receipt = session.record_answer("c-1", {"value": True}, now=clock.now)

    assert receipt.progress.answered == 1


# ── Scoring: after the end, and exactly once ──────────────────────────────


def test_an_unfinished_session_cannot_be_scored(store: SessionStore, clock: Clock):
    """Scoring a partial attempt reports a mark for unreached questions."""
    _, session = store.create(scope(), steps=plan(2))
    session.record_answer("c-1", {"value": True}, now=clock.now)

    with pytest.raises(SessionTransitionError) as caught:
        session.score_once(lambda completion: pytest.fail("the scorer was called"))

    assert caught.value.reason == "not_completed"
    assert session.snapshot().scored is False


def test_the_scorer_is_given_a_snapshot_it_cannot_write_back_through(
    store: SessionStore, clock: Clock
):
    """It gets the responses and both timestamps — as a copy, not as the session."""
    _, session = store.create(scope(), steps=plan(1))
    started = session.created_at
    clock.advance(120)
    session.record_answer("c-1", {"value": True}, now=clock.now)
    seen: dict = {}

    def compute(completion) -> dict:
        seen["timestamps"] = (completion.created_at, completion.completed_at)
        completion.responses["c-1"]["value"] = "tampered"
        completion.responses["c-2"] = {"value": False}
        return {"attempt_id": "attempt-1"}

    result = session.score_once(compute)

    assert result == {"attempt_id": "attempt-1"}
    assert seen["timestamps"] == (started, clock.now)
    assert session.responses == {"c-1": {"value": True}}, "the scorer wrote into the session"


def test_a_completion_snapshot_repr_does_not_contain_raw_responses(
    store: SessionStore, clock: Clock
):
    _, session = store.create(scope(), steps=plan(1))
    session.record_answer("c-1", {"text": "RAW-RESPONSE-CANARY"}, now=clock.now)
    rendered = []

    def compute(completion) -> dict:
        rendered.append(repr(completion))
        return {"attempt_id": "attempt-1"}

    session.score_once(compute)

    assert "RAW-RESPONSE-CANARY" not in rendered[0]


def test_a_successful_score_is_cached_and_the_scorer_never_runs_twice(
    store: SessionStore, clock: Clock
):
    """A refresh of the completion screen must not write a second attempt."""
    _, session = store.create(scope(), steps=plan(1))
    session.record_answer("c-1", {"value": True}, now=clock.now)
    calls = []

    def compute(completion) -> dict:
        calls.append(completion)
        return {"attempt_id": "attempt-1"}

    first = session.score_once(compute)
    second = session.score_once(compute)

    assert len(calls) == 1, "the durable scorer ran twice for one session"
    assert second == first
    assert second is not first, "a caller received the mutable cached object"
    assert session.snapshot().scored is True


def test_a_caller_cannot_mutate_the_cached_score(store: SessionStore, clock: Clock):
    _, session = store.create(scope(), steps=plan(1))
    session.record_answer("c-1", {"value": True}, now=clock.now)

    result = session.score_once(
        lambda completion: {"attempt_id": "attempt-1", "review": {"score": 1}}
    )
    result["attempt_id"] = "forged"
    result["review"]["score"] = 99

    assert session.score_once(lambda completion: pytest.fail("scored twice")) == {
        "attempt_id": "attempt-1",
        "review": {"score": 1},
    }


def test_concurrent_score_calls_run_one_durable_callback(store: SessionStore, clock: Clock):
    _, session = store.create(scope(), steps=plan(1))
    session.record_answer("c-1", {"value": True}, now=clock.now)
    first_entered = threading.Event()
    release_first = threading.Event()
    duplicate_entered = threading.Event()
    calls: list[int] = []
    results: list[dict] = []

    def compute(completion) -> dict:
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            first_entered.set()
            assert release_first.wait(timeout=5)
        else:
            duplicate_entered.set()
        return {"attempt_id": f"attempt-{len(calls)}"}

    first = threading.Thread(target=lambda: results.append(session.score_once(compute)))
    second = threading.Thread(target=lambda: results.append(session.score_once(compute)))
    first.start()
    assert first_entered.wait(timeout=5)
    second.start()

    assert not duplicate_entered.wait(timeout=0.2), "two score callbacks entered"
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert calls == [1]
    assert results == [{"attempt_id": "attempt-1"}, {"attempt_id": "attempt-1"}]


def test_a_failed_score_is_not_cached(store: SessionStore, clock: Clock):
    """A transient failure must not leave the session permanently unscorable."""
    _, session = store.create(scope(), steps=plan(1))
    session.record_answer("c-1", {"value": True}, now=clock.now)

    def unavailable(completion) -> dict:
        raise RuntimeError("the store was unavailable")

    with pytest.raises(RuntimeError):
        session.score_once(unavailable)

    assert session.snapshot().scored is False
    assert session.score_once(lambda completion: {"attempt_id": "attempt-2"}) == {
        "attempt_id": "attempt-2"
    }


def test_a_held_reference_cannot_score_after_hard_expiry(store: SessionStore, clock: Clock):
    _, session = store.create(scope(), steps=plan(1))
    session.record_answer("c-1", {"value": True}, now=clock.now)
    clock.advance(601)
    callbacks = []

    with pytest.raises(SessionTransitionError) as caught:
        session.score_once(lambda completion: callbacks.append("scored") or {"attempt_id": "late"})

    assert caught.value.reason == "session_retired"
    assert callbacks == []


# ── Resuming, which is what a reload is ───────────────────────────────────


def test_a_replacement_session_carries_every_lifecycle_field(store: SessionStore, clock: Clock):
    """A reload must hand back a token that works *and* remembers the learner."""
    _, first = store.create(scope(), steps=plan(2, reveal=("c-1",)))
    first.reveal("c-1", "my recall", lambda: "the back")
    first.record_answer("c-1", {"text": "my recall", "self_rating": "good"}, now=clock.now)
    first.record_answer("c-2", {"value": True}, now=clock.now)
    first.score_once(lambda completion: {"attempt_id": "attempt-1"})

    clock.advance(30)
    _, second = store.create(scope(), steps=plan(2, reveal=("c-1",)))
    second.resume_from(first)

    assert lifecycle(second) == lifecycle(first)
    assert second.score_once(lambda completion: pytest.fail("scored twice")) == {
        "attempt_id": "attempt-1"
    }


def test_resume_retires_the_predecessor_before_returning(store: SessionStore, clock: Clock):
    _, first = store.create(scope(), steps=plan(1))
    first.record_answer("c-1", {"value": True}, now=clock.now)
    _, second = store.create(scope(), steps=plan(1))

    second.resume_from(first)

    with pytest.raises(SessionTransitionError) as caught:
        first.score_once(lambda completion: {"attempt_id": "stale"})
    assert caught.value.reason == "session_retired"
    assert second.score_once(lambda completion: {"attempt_id": "attempt-1"}) == {
        "attempt_id": "attempt-1"
    }


@pytest.mark.parametrize(
    "replacement_scope,replacement_steps",
    [
        (scope(), plan(1)),
        (scope(user="2002"), plan(2)),
    ],
)
def test_resume_refuses_a_different_scope_or_plan_without_mutating(
    store: SessionStore,
    clock: Clock,
    replacement_scope: SessionScope,
    replacement_steps: tuple[SessionStep, ...],
):
    _, first = store.create(scope(), steps=plan(2))
    first.record_answer("c-1", {"value": True}, now=clock.now)
    _, second = store.create(replacement_scope, steps=replacement_steps)
    before = lifecycle(second)

    with pytest.raises(ValueError, match="same scope and ordered plan"):
        second.resume_from(first)

    assert lifecycle(second) == before


def test_resume_refuses_a_hard_retired_predecessor(store: SessionStore, clock: Clock):
    _, first = store.create(scope(), steps=plan(2))
    first.record_answer("c-1", {"value": True}, now=clock.now)
    first.retire()
    _, second = store.create(scope(), steps=plan(2))
    before = lifecycle(second)

    with pytest.raises(ValueError, match="predecessor is not resumable"):
        second.resume_from(first)

    assert lifecycle(second) == before


def test_resume_checks_the_predecessor_deadline_at_transfer_time(store: SessionStore, clock: Clock):
    _, first = store.create(scope(), steps=plan(2))
    first.record_answer("c-1", {"value": True}, now=clock.now)
    clock.advance(599)
    _, second = store.create(scope(), steps=plan(2))
    clock.advance(2)
    before = lifecycle(second)

    with pytest.raises(ValueError, match="predecessor is not resumable"):
        second.resume_from(first)

    assert lifecycle(second) == before


def test_resume_refuses_to_overwrite_a_nonfresh_replacement(store: SessionStore, clock: Clock):
    _, first = store.create(scope(), steps=plan(2))
    first.record_answer("c-1", {"value": True}, now=clock.now)
    _, second = store.create(scope(), steps=plan(2))
    second.record_answer("c-1", {"value": False}, now=clock.now)
    before = lifecycle(second)

    with pytest.raises(ValueError, match="replacement session must be fresh"):
        second.resume_from(first)

    assert lifecycle(second) == before


def test_resume_waits_for_an_in_flight_score_and_carries_its_result(
    store: SessionStore, clock: Clock
):
    _, first = store.create(scope(), steps=plan(1))
    first.record_answer("c-1", {"value": True}, now=clock.now)
    _, second = store.create(scope(), steps=plan(1))
    scorer_entered = threading.Event()
    release_scorer = threading.Event()
    resume_finished = threading.Event()

    def score() -> None:
        def compute(completion) -> dict:
            scorer_entered.set()
            assert release_scorer.wait(timeout=5)
            return {"attempt_id": "attempt-1"}

        first.score_once(compute)

    score_thread = threading.Thread(target=score)
    resume_thread = threading.Thread(
        target=lambda: (second.resume_from(first), resume_finished.set())
    )
    score_thread.start()
    assert scorer_entered.wait(timeout=5)
    resume_thread.start()

    assert not resume_finished.wait(timeout=0.2), "resume copied a half-committed score"
    release_scorer.set()
    score_thread.join(timeout=5)
    resume_thread.join(timeout=5)

    assert second.score_once(lambda completion: pytest.fail("scored twice")) == {
        "attempt_id": "attempt-1"
    }


def test_resume_waits_for_an_in_flight_answer_and_carries_it(store: SessionStore, clock: Clock):
    _, first = store.create(scope(), steps=plan(2))
    _, second = store.create(scope(), steps=plan(2))
    copy_entered = threading.Event()
    release_copy = threading.Event()
    resume_finished = threading.Event()

    class BlockingValue:
        def __deepcopy__(self, memo):
            copy_entered.set()
            assert release_copy.wait(timeout=5)
            return True

    answer_thread = threading.Thread(
        target=lambda: first.record_answer("c-1", {"value": BlockingValue()}, now=clock.now)
    )
    resume_thread = threading.Thread(
        target=lambda: (second.resume_from(first), resume_finished.set())
    )
    answer_thread.start()
    assert copy_entered.wait(timeout=5)
    resume_thread.start()

    assert not resume_finished.wait(timeout=0.2), "resume copied a half-committed answer"
    release_copy.set()
    answer_thread.join(timeout=5)
    resume_thread.join(timeout=5)

    assert second.snapshot().progress.position == 1
    assert second.responses == {"c-1": {"value": True}}


def test_resuming_does_not_copy_identity_freshness_or_lifetime(store: SessionStore, clock: Clock):
    """Everything that bounds a session is the *new* session's own."""
    _, first = store.create(scope(), steps=plan(2), auth_date=1000)
    first.record_answer("c-1", {"value": True}, now=clock.now)

    clock.advance(60)
    _, second = store.create(scope(), steps=plan(2), auth_date=1060)
    second.resume_from(first)

    assert second.snapshot().progress.position == 1
    assert second.ref != first.ref
    assert second.auth_date == 1060
    assert second.created_at == clock.now
    assert second.expires_at > first.expires_at


def test_a_resumed_session_and_the_one_it_replaced_share_no_state(
    store: SessionStore, clock: Clock
):
    """Carrying state forward must not leave the two writing through each other."""
    _, first = store.create(scope(), steps=plan(2, reveal=("c-2",)))
    first.record_answer("c-1", {"choices": ["a"]}, now=clock.now)
    _, second = store.create(scope(), steps=plan(2, reveal=("c-2",)))
    second.resume_from(first)

    second.reveal("c-2", "my recall", lambda: "the back")
    second.record_answer("c-2", {"text": "my recall", "self_rating": "good"}, now=clock.now)
    second.score_once(lambda completion: {"attempt_id": "attempt-1"})

    assert lifecycle(first) == {
        "view": SessionView(
            progress=SessionProgress(position=1, component_count=2, answered=1, completed=False),
            current_component_id="c-2",
            answered_component_ids=("c-1",),
            completed_at=None,
            scored=False,
        ),
        "responses": {"c-1": {"choices": ["a"]}},
        "revealed": {},
    }


def test_an_unscored_session_resumes_without_inventing_a_result(store: SessionStore, clock: Clock):
    _, first = store.create(scope(), steps=plan(2))
    first.record_answer("c-1", {"value": True}, now=clock.now)
    _, second = store.create(scope(), steps=plan(2))

    second.resume_from(first)

    assert second.snapshot().scored is False
    with pytest.raises(SessionTransitionError) as caught:
        second.score_once(lambda completion: pytest.fail("scored an unfinished session"))
    assert caught.value.reason == "not_completed"


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
    store.create(scope(), steps=plan(1))
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

    store.create(scope(), steps=plan(1))

    assert store.last_activity_at is None
