"""Consent that comes from the platform, not from the model's account of it.

The property under test is one sentence: **an exercise cannot be opened on
words nobody wrote.** Every test below is a way that used to be possible.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from learning_studio import consent
from learning_studio.evidence import (
    EvidenceKey,
    EvidenceStore,
    capture_message_evidence,
    normalise,
)


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def key(**overrides) -> EvidenceKey:
    fields = {
        "profile": "default",
        "platform": "telegram",
        "chat_id": "1001",
        "thread_id": "",
        "user_id": "1001",
        "message_id": "555",
    }
    fields.update(overrides)
    return EvidenceKey(**fields)


@pytest.fixture(autouse=True)
def _empty_global_store():
    """The process-wide store is shared; no test may leave anything in it.

    Two tests below exercise the hook against the real global store on purpose
    — that is the object Hermes will call into — so without this they would
    leak a learner's message into every later test in the session.
    """
    from learning_studio.evidence import STORE

    STORE.clear()
    yield
    STORE.clear()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock) -> EvidenceStore:
    return EvidenceStore(clock=clock)


@pytest.fixture
def bound_session(monkeypatch):
    """The session variables a Telegram DM turn actually carries."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "1001")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "1001")
    monkeypatch.setenv("HERMES_SESSION_MESSAGE_ID", "555")


def decide(store, quote, *, initiation="learner_request", confirmed=True):
    return consent.decide(
        profile="default",
        initiation=initiation,
        learner_confirmed=confirmed,
        learner_quote=quote,
        store=store,
    )


# ── The store ─────────────────────────────────────────────────────────────


def test_a_quotation_from_the_recorded_message_matches(store):
    store.record(key(), "Can you quiz me on photosynthesis?")

    assert store.state(key(), "quiz me on photosynthesis") == "matched"


def test_matching_ignores_case_and_spacing_but_not_content(store):
    store.record(key(), "Can you   QUIZ me\non photosynthesis?")

    assert store.state(key(), "quiz me on photosynthesis") == "matched"
    assert store.state(key(), "quiz me on mitosis") == "mismatched"


def test_a_fabricated_quotation_does_not_match(store):
    store.record(key(), "I am too tired for this today")

    assert store.state(key(), "yes please, quiz me") == "mismatched"


def test_a_quotation_from_another_message_does_not_match(store):
    store.record(key(message_id="554"), "quiz me on photosynthesis")

    assert store.state(key(message_id="555"), "quiz me on photosynthesis") == "absent"


@pytest.mark.parametrize(
    "other",
    [
        {"user_id": "2002"},
        {"chat_id": "-100777"},
        {"profile": "family"},
        {"platform": "discord"},
        {"thread_id": "9"},
    ],
)
def test_evidence_is_scoped_to_one_identity(store, other: dict):
    store.record(key(), "quiz me on photosynthesis")

    assert store.state(key(**other), "quiz me on photosynthesis") == "absent"


def test_evidence_expires(store, clock):
    store.record(key(), "quiz me on photosynthesis")
    clock.advance(601)

    assert store.state(key(), "quiz me on photosynthesis") == "absent"


def test_a_short_quotation_proves_nothing(store):
    store.record(key(), "ok")

    assert store.state(key(), "ok") == "unusable"


def test_an_incomplete_key_is_never_usable(store):
    store.record(key(message_id=""), "quiz me on photosynthesis")

    assert store.state(key(message_id=""), "quiz me on photosynthesis") == "unusable"
    assert len(store) == 0


def test_the_store_is_bounded(clock):
    store = EvidenceStore(clock=clock, max_entries=3)

    for index in range(10):
        store.record(key(message_id=str(index)), f"message number {index} please")

    assert len(store) == 3


# ── Spending, and the replay defence ──────────────────────────────────────


def test_a_message_can_be_spent_once(store):
    store.record(key(), "quiz me on photosynthesis")

    assert store.spend(key()) is True
    assert store.spend(key()) is False


def test_a_spent_message_stays_spent_rather_than_becoming_absent(store, clock):
    """The bug this replaces: expiry used to make a used quotation new again.

    The old ledger deleted its own entry when it went stale and then refused.
    The next call found nothing, concluded the consent was fresh, and launched.
    """
    store.record(key(), "quiz me on photosynthesis")
    store.spend(key())

    clock.advance(700)  # past the evidence TTL, inside the tombstone's

    assert store.state(key(), "quiz me on photosynthesis") == "spent"


def test_a_spent_message_cannot_be_re_armed_by_a_redelivered_update(store):
    store.record(key(), "quiz me on photosynthesis")
    store.spend(key())

    store.record(key(), "quiz me on photosynthesis")

    assert store.state(key(), "quiz me on photosynthesis") == "spent"


def test_concurrent_spends_admit_exactly_one(store):
    """Two launches racing on one message; the spend is the arbitration."""
    import threading

    store.record(key(), "quiz me on photosynthesis")
    winners: list[bool] = []
    start = threading.Barrier(8)

    def attempt() -> None:
        start.wait()
        winners.append(store.spend(key()))

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert winners.count(True) == 1


# ── The policy ────────────────────────────────────────────────────────────


def test_a_request_backed_by_the_current_message_may_launch(store, bound_session):
    store.record(key(), "can you quiz me on photosynthesis")

    decided = decide(store, "quiz me on photosynthesis")

    assert decided.may_create is True


def test_a_fabricated_request_is_refused(store, bound_session):
    """The headline case: the model asserts a request nobody made."""
    store.record(key(), "what does chlorophyll actually do?")

    with pytest.raises(consent.ConsentRequired) as caught:
        decide(store, "quiz me on photosynthesis")

    assert caught.value.reason == "quote_not_in_current_message"


def test_a_turn_with_no_learner_message_is_refused(store, monkeypatch):
    """A cron job, a background task, or a turn that has moved on."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "1001")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "1001")
    monkeypatch.delenv("HERMES_SESSION_MESSAGE_ID", raising=False)

    with pytest.raises(consent.ConsentRequired) as caught:
        decide(store, "quiz me on photosynthesis")

    assert caught.value.reason == "quote_unusable"


def test_evidence_from_a_previous_turn_is_refused(store, bound_session, monkeypatch):
    store.record(key(message_id="554"), "quiz me on photosynthesis")
    monkeypatch.setenv("HERMES_SESSION_MESSAGE_ID", "555")

    with pytest.raises(consent.ConsentRequired) as caught:
        decide(store, "quiz me on photosynthesis")

    assert caught.value.reason == "no_current_learner_message"


def test_a_suggestion_without_the_confirmation_flag_is_refused(store, bound_session):
    store.record(key(), "go on then, that sounds useful")

    with pytest.raises(consent.ConsentRequired) as caught:
        decide(store, "go on then", initiation="agent_suggestion", confirmed=False)

    assert caught.value.reason == "suggestion_not_confirmed"


def test_a_suggestion_the_learner_agreed_to_may_launch(store, bound_session):
    store.record(key(), "go on then, that sounds useful")

    decided = decide(store, "go on then", initiation="agent_suggestion")

    assert decided.may_create is True


def test_a_spent_message_may_report_but_not_relaunch(store, bound_session):
    store.record(key(), "quiz me on photosynthesis")
    first = decide(store, "quiz me on photosynthesis")
    consent.spend(first, store=store)

    second = decide(store, "quiz me on photosynthesis")

    assert second.may_create is False


def test_a_stale_quotation_fails_every_time_not_just_the_first(store, bound_session, clock):
    """The exact regression: the old ledger let the second attempt through."""
    store.record(key(), "quiz me on photosynthesis")
    first = decide(store, "quiz me on photosynthesis")
    consent.spend(first, store=store)
    clock.advance(700)

    for _ in range(3):
        assert decide(store, "quiz me on photosynthesis").may_create is False


def test_the_stated_basis_describes_a_check_not_a_state_of_mind(store, bound_session):
    """The response must not report the model's assertion as this plugin's finding."""
    store.record(key(), "go on then, that sounds useful")

    decided = decide(store, "go on then", initiation="agent_suggestion")

    assert "you read them as" in decided.basis
    assert "the learner agreed" not in decided.basis.lower()


# ── The hook ──────────────────────────────────────────────────────────────


@dataclass
class Source:
    platform: str = "telegram"
    chat_id: str = "1001"
    chat_type: str = "dm"
    user_id: str = "1001"
    thread_id: str | None = None
    is_bot: bool = False


@dataclass
class Event:
    text: str
    source: Source
    message_id: str | None = "555"


@pytest.fixture
def allowlisted(monkeypatch):
    """The learner in :class:`Source` is allowed to use the Studio."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "1001,2002")


def test_the_hook_records_an_incoming_message(hermes_home, allowlisted, monkeypatch):
    from learning_studio import evidence as evidence_module

    store = EvidenceStore()
    monkeypatch.setattr(evidence_module, "STORE", store)

    assert capture_message_evidence(event=Event("quiz me on photosynthesis", Source())) is None
    assert store.state(key(), "quiz me on photosynthesis") == "matched"


# ── Only what could ever authorise a launch is kept ───────────────────────


@pytest.mark.parametrize(
    "source",
    [
        Source(platform="slack"),
        Source(platform="discord"),
        Source(chat_type="group"),
        Source(chat_type="supergroup"),
        Source(chat_type="channel"),
        Source(chat_type=""),
        Source(user_id="9999"),
    ],
    ids=["slack", "discord", "group", "supergroup", "channel", "no-chat-type", "not-allowlisted"],
)
def test_the_hook_keeps_nothing_a_launch_could_never_use(
    hermes_home, allowlisted, monkeypatch, source
):
    """The store is small, and everything it holds evicts something else.

    It used to record every message the gateway saw, on every platform, from
    every chat, from anybody. None of that could ever authorise a launch — a
    launch goes to an allowlisted Telegram DM and nowhere else — but all of it
    took space, so strangers talking in a room could push out the "yes" a
    learner had just written in their own conversation.
    """
    from learning_studio import evidence as evidence_module

    store = EvidenceStore()
    monkeypatch.setattr(evidence_module, "STORE", store)

    capture_message_evidence(event=Event("quiz me on photosynthesis", source))

    assert len(store) == 0


def test_unrelated_traffic_cannot_evict_a_learners_own_message(
    hermes_home, allowlisted, monkeypatch
):
    """The eviction this filter exists to prevent, run end to end."""
    from learning_studio import evidence as evidence_module

    store = EvidenceStore()
    monkeypatch.setattr(evidence_module, "STORE", store)

    capture_message_evidence(event=Event("quiz me on photosynthesis", Source()))

    for index in range(evidence_module.MAX_ENTRIES * 3):
        capture_message_evidence(
            event=Event(
                "chatter",
                Source(chat_type="group", chat_id=f"-100{index}", user_id="1001"),
                message_id=str(9000 + index),
            )
        )

    assert store.state(key(), "quiz me on photosynthesis") == "matched"


def test_the_hook_ignores_a_bot_message(hermes_home, monkeypatch):
    from learning_studio import evidence as evidence_module

    store = EvidenceStore()
    monkeypatch.setattr(evidence_module, "STORE", store)

    capture_message_evidence(event=Event("quiz me on photosynthesis", Source(is_bot=True)))

    assert len(store) == 0


def test_the_hook_ignores_an_event_with_no_message_id(hermes_home, monkeypatch):
    from learning_studio import evidence as evidence_module

    store = EvidenceStore()
    monkeypatch.setattr(evidence_module, "STORE", store)

    capture_message_evidence(event=Event("quiz me", Source(), message_id=None))

    assert len(store) == 0


@pytest.mark.parametrize("event", [None, object(), Event("x", None)])  # type: ignore[arg-type]
def test_the_hook_never_raises_on_an_unexpected_shape(hermes_home, event):
    """A plugin that could break message dispatch is one nobody should enable."""
    assert capture_message_evidence(event=event) is None


def test_the_hook_returns_none_so_dispatch_continues(hermes_home):
    assert capture_message_evidence(event=Event("hello there", Source())) is None


# ── Privacy ───────────────────────────────────────────────────────────────


def test_nothing_a_learner_wrote_is_returned_to_the_caller(store, bound_session):
    store.record(key(), "I keep confusing mitosis and meiosis, quiz me")

    decided = decide(store, "quiz me")

    assert "mitosis" not in decided.basis
    assert "mitosis" not in repr(decided.key)


def test_a_refusal_never_quotes_the_message_or_the_attempt(store, bound_session):
    store.record(key(), "my password is hunter2 and I am tired")

    with pytest.raises(consent.ConsentRequired) as caught:
        decide(store, "quiz me on photosynthesis")

    assert "hunter2" not in str(caught.value)
    assert "photosynthesis" not in str(caught.value)


def test_the_store_writes_nothing_to_disk(hermes_home, store):
    store.record(key(), "quiz me on photosynthesis")
    store.spend(key())

    assert not (hermes_home / "workspace").exists()


def test_normalisation_keeps_content_and_drops_only_shape():
    assert normalise("  Quiz   ME\non\tthis  ") == "quiz me on this"


def test_an_injected_store_is_used_even_once_it_is_empty(store, bound_session):
    """Regression: an empty store must not fall through to the global one.

    ``EvidenceStore`` defines ``__len__``, so an empty one is falsy. Selecting
    the default with ``store or STORE`` therefore switched stores the instant
    spending emptied the injected one — and a message that had just been used
    read as never-used, because the question was being asked of a different
    object.
    """
    from learning_studio import evidence as evidence_module

    store.record(key(), "quiz me on photosynthesis")
    consent.spend(decide(store, "quiz me on photosynthesis"), store=store)

    assert len(store) == 0
    assert len(evidence_module.STORE) == 0
    assert decide(store, "quiz me on photosynthesis").may_create is False


# ── Eviction must not re-arm a message that was already used ──────────────


def test_a_tombstone_evicted_by_traffic_does_not_re_arm_the_message(store):
    """The bounded table forgets; the conversation does not.

    A tombstone names one message and there is room for a fixed number of
    them. Spend enough messages and the oldest tombstone goes — and a
    redelivery of that Telegram update then looked like something nobody had
    used, which is a replayable consent. One integer per conversation answers
    the same question for every message below it, and does not evict.
    """
    from learning_studio import evidence as evidence_module

    first = key(message_id="500")
    store.record(first, "quiz me on photosynthesis")
    assert store.spend(first) is True

    for index in range(evidence_module.MAX_TOMBSTONES + 50):
        later = key(message_id=str(100_000 + index))
        store.record(later, "another message entirely")
        store.spend(later)

    assert first not in store._spent, "the tombstone survived; the test proves nothing"

    store.record(first, "quiz me on photosynthesis")

    assert store.state(first, "quiz me on photosynthesis") == "spent"
    assert store.reserve(first) is False


def test_every_earlier_message_in_the_conversation_is_covered(store):
    """One watermark stands for all of them, which is why it is one integer."""
    used = key(message_id="900")
    store.record(used, "quiz me on photosynthesis")
    assert store.spend(used) is True

    earlier = key(message_id="42")
    store.record(earlier, "quiz me on photosynthesis")

    assert store.state(earlier, "quiz me on photosynthesis") == "spent"

    # A *newer* message is untouched: this bounds replay, not conversation.
    newer = key(message_id="901")
    store.record(newer, "quiz me on photosynthesis")
    assert store.state(newer, "quiz me on photosynthesis") == "matched"


def test_the_watermark_is_per_conversation(store):
    """Another chat's numbering says nothing about this one's."""
    theirs = key(message_id="900", chat_id="2002", user_id="2002")
    store.record(theirs, "quiz me on photosynthesis")
    assert store.spend(theirs) is True

    mine = key(message_id="42")
    store.record(mine, "quiz me on photosynthesis")

    assert store.state(mine, "quiz me on photosynthesis") == "matched"


def test_a_platform_without_ordered_ids_still_works(store):
    """No order means no inference, and no silent refusal of valid messages."""
    first = key(message_id="msg-abc")
    store.record(first, "quiz me on photosynthesis")
    assert store.spend(first) is True

    second = key(message_id="msg-def")
    store.record(second, "quiz me on photosynthesis")

    assert store.state(second, "quiz me on photosynthesis") == "matched"
