"""Memory candidates: what may be proposed, and the much longer list of what may not.

Every rejection here protects a person from a wrong permanent record. The
asymmetry is deliberate and worth restating: failing to remember something
costs one question next session; remembering something false about someone —
especially about their health or their difficulties — can be uncorrectable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from learning_studio import service
from learning_studio.candidates import CandidateRejected, propose
from learning_studio.config import LearningStudioConfig
from learning_studio.identity import Principal

LEARNER = Principal(
    profile="default", platform="telegram", user_id="4004", source="gateway_session"
)

CONSENT = {
    "consent_statement": "please remember I need captions",
    "needs": ["captions on all audio"],
}


def _propose(**overrides):
    payload = {
        "category": "durable_preference",
        "statement": "Prefers worked examples before attempting a problem.",
        "evidence_summary": "Said so directly when asked how they like to start.",
        "origin": "explicit_durable_preference",
    }
    payload.update(overrides)
    return propose(**payload)


# ── Origins that may produce a candidate ──────────────────────────────────


def test_an_explicit_durable_preference_is_accepted():
    candidate = _propose()

    assert candidate.recommended_action.value == "add"
    assert candidate.confirmation_state.value == "unconfirmed"


def test_a_confirmed_long_term_goal_is_accepted():
    candidate = _propose(
        category="long_term_goal",
        statement="Working toward reading technical papers without a dictionary.",
        evidence_summary="Confirmed as an ongoing goal when the track was set up.",
        origin="confirmed_long_term_goal",
    )

    assert candidate.category.value == "long_term_goal"


def test_repeated_evidence_is_accepted_once_the_pattern_holds():
    candidate = _propose(
        origin="repeated_evidence",
        statement="Consistently prefers short sessions over long ones.",
        evidence_summary="Ended four of five sessions at around fifteen minutes.",
        evidence_count=4,
        min_evidence=3,
    )

    assert candidate.origin.value == "repeated_evidence"


def test_repeated_evidence_below_the_threshold_is_refused():
    with pytest.raises(CandidateRejected, match="at least 3 independent observations"):
        _propose(
            origin="repeated_evidence",
            statement="Prefers short sessions.",
            evidence_summary="Ended one session early.",
            evidence_count=1,
            min_evidence=3,
        )


def test_a_correction_may_replace_a_named_entry():
    candidate = _propose(
        origin="explicit_correction",
        statement="Prefers detailed feedback, not brief feedback.",
        evidence_summary="Corrected an earlier note during this session.",
        recommended_action="replace",
        replaces="Prefers brief feedback.",
    )

    assert candidate.recommended_action.value == "replace"
    assert candidate.replaces == "Prefers brief feedback."


def test_replace_without_a_target_is_refused():
    """A contradiction left beside the entry it contradicts is worse than neither."""
    with pytest.raises(CandidateRejected, match="requires 'replaces'"):
        _propose(origin="explicit_correction", recommended_action="replace")


def test_a_withdrawal_produces_a_removal_candidate():
    candidate = _propose(
        origin="explicit_withdrawal",
        statement="No longer wants their target level remembered.",
        evidence_summary="Asked for it to be forgotten.",
        recommended_action="remove",
    )

    assert candidate.recommended_action.value == "remove"


# ── Origins that must never produce a candidate ───────────────────────────


@pytest.mark.parametrize(
    "origin",
    [
        "single_error",
        "single_slow_response",
        "single_inference",
        "temporary_frustration",
        "raw_score",
        "raw_attempts",
        "session_state",
    ],
)
def test_forbidden_origins_are_refused_with_a_reason(origin: str):
    with pytest.raises(CandidateRejected) as exc:
        _propose(origin=origin)

    assert origin in str(exc.value)
    assert len(str(exc.value)) > len(origin) + 20, "the refusal must explain itself"


def test_no_candidate_from_one_error():
    with pytest.raises(CandidateRejected, match="not a durable fact"):
        _propose(
            origin="single_error",
            statement="Struggles with recursion.",
            evidence_summary="Got one recursion question wrong.",
        )


def test_no_candidate_from_one_slow_response():
    with pytest.raises(CandidateRejected, match="latency"):
        _propose(
            origin="single_slow_response",
            statement="Weak on this material.",
            evidence_summary="Took 40 seconds to answer once.",
        )


def test_an_unknown_origin_is_refused():
    with pytest.raises(CandidateRejected, match="origin must be one of"):
        _propose(origin="because_i_felt_like_it")


# ── Content that must never be stored ─────────────────────────────────────


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("Their API key is stored in the project.", "credential"),
        ("Session id 12345 tracks their progress.", "session_id"),
        ("Their initData hash= confirms the user.", "telegram_init_data"),
        ("Reachable at https://abc-def.trycloudflare.com for now.", "tunnel_url"),
        ("Keep the full transcript of the session.", "transcript"),
        ("They scored 7/10 on the verb drill.", "raw_score"),
        ("The learner answered with: mitochondria.", "raw_answer"),
        ("Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 is their token.", "credential"),
    ],
)
def test_forbidden_content_is_refused(statement: str, expected: str):
    with pytest.raises(CandidateRejected) as exc:
        _propose(statement=statement)

    assert expected in str(exc.value)


def test_raw_attempts_in_the_evidence_summary_are_refused():
    """The evidence field is scanned as carefully as the statement."""
    with pytest.raises(CandidateRejected, match="transcript"):
        _propose(evidence_summary="See the full conversation from Tuesday.")


def test_an_inferred_diagnosis_is_refused():
    with pytest.raises(CandidateRejected, match="sensitive health, disability"):
        _propose(
            statement="The learner seems to have dyslexia.",
            evidence_summary="Reads slowly and reverses letters.",
        )


def test_a_sensitive_trait_outside_the_accessibility_category_is_refused():
    with pytest.raises(CandidateRejected, match="sensitive health, disability"):
        _propose(
            category="durable_preference",
            statement="Has ADHD, so keep sessions short.",
            evidence_summary="Mentioned it in passing.",
        )


# ── Accessibility consent ─────────────────────────────────────────────────


def test_an_accessibility_candidate_cannot_come_from_repeated_evidence():
    """A pattern across exercises is not evidence that someone has a condition."""
    with pytest.raises(CandidateRejected, match="cannot come from 'repeated_evidence'"):
        _propose(
            category="accessibility",
            statement="The learner has ADHD",
            evidence_summary="Observed this pattern in three independent exercises",
            origin="repeated_evidence",
            confirmation_state="learner_confirmed",
            evidence_count=5,
            min_evidence=3,
            consent_statement="ok",
        )


def test_an_unconfirmed_accessibility_candidate_is_refused():
    with pytest.raises(CandidateRejected, match="must be 'learner_confirmed'"):
        _propose(
            category="accessibility",
            statement="Needs captions on any audio material.",
            evidence_summary="Asked for captions this session.",
            confirmation_state="unconfirmed",
            consent_statement="please remember this",
        )


def test_an_accessibility_candidate_without_a_consent_statement_is_refused():
    """A generic yes elsewhere in the request is not consent for this fact."""
    with pytest.raises(CandidateRejected, match="requires 'consent_statement'"):
        _propose(
            category="accessibility",
            statement="Needs captions on any audio material.",
            evidence_summary="Asked for this to be remembered.",
            confirmation_state="learner_confirmed",
        )


def test_accessibility_is_accepted_when_confirmed_and_consented():
    candidate = _propose(
        category="accessibility",
        statement="Needs captions on any audio material.",
        evidence_summary="Asked for this to be remembered for future sessions.",
        confirmation_state="learner_confirmed",
        consent_statement="yes, please remember I need captions",
    )

    assert candidate.category.value == "accessibility"
    assert candidate.consent_reference == "yes, please remember I need captions"


def test_a_sensitive_statement_cannot_be_relabelled_to_slip_past_the_scan():
    """The structural gate, not the regex, is what enforces the boundary."""
    with pytest.raises(CandidateRejected, match="sensitive health, disability"):
        _propose(
            category="durable_preference",
            statement="Requires extra time because of their diagnosis.",
            evidence_summary="Came up in conversation.",
        )


def test_a_privacy_refusal_can_be_recorded_as_a_narrow_preference():
    candidate = _propose(
        category="privacy_preference",
        statement="Does not want their target level remembered between sessions.",
        evidence_summary="Declined when asked.",
        origin="explicit_withdrawal",
    )

    assert candidate.category.value == "privacy_preference"


# ── Through the service ───────────────────────────────────────────────────


def test_an_accepted_candidate_is_stored_and_returned(hermes_home: Path):
    result = service.save_context(
        principal=LEARNER,
        memory_candidates=[
            {
                "category": "durable_preference",
                "statement": "Prefers worked examples first.",
                "evidence_summary": "Said so directly.",
                "origin": "explicit_durable_preference",
            }
        ],
    )

    accepted = result["outcome"]["memory_candidates"]["accepted"]
    assert len(accepted) == 1
    assert accepted[0]["statement"] == "Prefers worked examples first."

    stored = service.get_context(principal=LEARNER, include_memory_candidates=True)
    assert len(stored["memory_candidates"]) == 1


def test_a_rejected_candidate_is_reported_with_its_reason_not_stored(hermes_home: Path):
    result = service.save_context(
        principal=LEARNER,
        memory_candidates=[
            {
                "category": "durable_preference",
                "statement": "Struggles with recursion.",
                "evidence_summary": "One wrong answer.",
                "origin": "single_error",
            }
        ],
    )

    rejected = result["outcome"]["memory_candidates"]["rejected"]
    assert len(rejected) == 1
    assert "not a durable fact" in rejected[0]["reason"]

    stored = service.get_context(principal=LEARNER, include_memory_candidates=True)
    assert stored["memory_candidates"] == []


def test_one_bad_candidate_does_not_discard_the_good_ones(hermes_home: Path):
    result = service.save_context(
        principal=LEARNER,
        memory_candidates=[
            {
                "category": "durable_preference",
                "statement": "Prefers worked examples first.",
                "evidence_summary": "Said so directly.",
                "origin": "explicit_durable_preference",
            },
            {
                "category": "durable_preference",
                "statement": "Bad one.",
                "evidence_summary": "One wrong answer.",
                "origin": "single_error",
            },
        ],
    )

    candidates = result["outcome"]["memory_candidates"]
    assert len(candidates["accepted"]) == 1
    assert len(candidates["rejected"]) == 1


def test_the_response_never_claims_hermes_memory_was_updated(hermes_home: Path):
    result = service.save_context(
        principal=LEARNER,
        memory_candidates=[
            {
                "category": "durable_preference",
                "statement": "Prefers worked examples first.",
                "evidence_summary": "Said so directly.",
                "origin": "explicit_durable_preference",
            }
        ],
    )

    assert result["hermes_memory_updated"] is False
    assert "does not read or write Hermes memory" in result["note"]


def test_accessibility_needs_are_never_written_to_storage_without_consent(hermes_home: Path):
    """Session-only means absent from SQLite, not merely short-lived in it.

    A row with a 72-hour TTL is a record. The whole point of "session-only"
    is that no record exists.
    """
    from learning_studio import storage

    result = service.save_context(
        principal=LEARNER,
        temporary_context={"accessibility_needs": ["ADHD accommodations"]},
    )

    with storage.connect() as conn:
        values = conn.execute(
            "SELECT COUNT(*) AS n FROM context_values WHERE field = 'accessibility_needs'"
        ).fetchone()["n"]
        revisions = conn.execute(
            "SELECT COUNT(*) AS n FROM context_revisions WHERE field = 'accessibility_needs'"
        ).fetchone()["n"]

    assert values == 0, "an accessibility value was written to SQLite without consent"
    assert revisions == 0, "an accessibility revision was written to SQLite without consent"
    assert any(item["field"] == "accessibility_needs" for item in result["outcome"]["not_stored"])


def test_the_response_says_the_need_was_not_stored(hermes_home: Path):
    """The agent must not believe it can read this back later."""
    result = service.save_context(
        principal=LEARNER, temporary_context={"accessibility_needs": ["captions on all audio"]}
    )

    reason = result["outcome"]["not_stored"][0]["reason"]
    assert "NOT stored" in reason
    assert "current_request" in reason


def test_an_unstored_need_is_absent_from_a_later_read(hermes_home: Path):
    service.save_context(
        principal=LEARNER, temporary_context={"accessibility_needs": ["captions on all audio"]}
    )

    result = service.get_context(principal=LEARNER)

    assert "accessibility_needs" not in result["temporary_context"]


def test_an_accessibility_need_is_honoured_through_the_current_request(hermes_home: Path):
    """Refusing to store a need must not mean ignoring it."""
    result = service.get_context(
        principal=LEARNER, current_request={"accessibility_needs": ["captions on all audio"]}
    )

    resolved = result["resolved_context"]["accessibility_needs"]
    assert resolved["value"] == ["captions on all audio"]
    assert resolved["provenance"] == "explicit_request"


def test_a_consented_need_is_stored(hermes_home: Path):
    service.save_context(
        principal=LEARNER,
        temporary_context={"accessibility_needs": ["captions on all audio"]},
        accessibility_consent=CONSENT,
    )

    result = service.get_context(principal=LEARNER)
    assert result["temporary_context"]["accessibility_needs"]["value"] == ["captions on all audio"]


def test_consent_for_one_need_does_not_store_a_different_one(hermes_home: Path):
    """Permission is per-fact. A blanket yes was the defect being fixed."""
    result = service.save_context(
        principal=LEARNER,
        temporary_context={"accessibility_needs": ["a screen reader"]},
        accessibility_consent=CONSENT,
    )

    assert any(item["field"] == "accessibility_needs" for item in result["outcome"]["not_stored"])
    stored = service.get_context(principal=LEARNER)["temporary_context"]
    assert "accessibility_needs" not in stored


def test_an_operator_may_block_durable_accessibility_entirely(hermes_home: Path):
    """A shared or managed profile can refuse even on request."""
    config = LearningStudioConfig.from_mapping(
        {"learning_studio": {"allow_durable_accessibility_needs": False}}
    )

    with pytest.raises(service.ConsentError, match="configured never to store"):
        service.save_context(
            principal=LEARNER,
            temporary_context={"accessibility_needs": ["captions on all audio"]},
            accessibility_consent=CONSENT,
            config=config,
        )


def test_accessibility_is_dropped_from_a_bulk_track_context_write(hermes_home: Path):
    """The consent gate cannot be bypassed by bundling it with other fields."""
    result = service.save_context(
        principal=LEARNER,
        track={
            "name": "T",
            "confirmed": True,
            "context": {"goal": "stored", "accessibility_needs": ["not consented"]},
        },
    )

    track_id = result["outcome"]["track"]["track_id"]
    confirmed = service.get_context(principal=LEARNER, track_id=track_id)["confirmed_context"]
    assert "goal" in confirmed
    assert "accessibility_needs" not in confirmed
    assert any(item["field"] == "accessibility_needs" for item in result["outcome"]["not_stored"])


def test_the_evidence_threshold_follows_configuration(hermes_home: Path):
    config = LearningStudioConfig.from_mapping(
        {"learning_studio": {"memory_candidate_min_evidence": 5}}
    )

    result = service.save_context(
        principal=LEARNER,
        memory_candidates=[
            {
                "category": "durable_preference",
                "statement": "Prefers short sessions.",
                "evidence_summary": "Ended sessions early repeatedly.",
                "origin": "repeated_evidence",
                "evidence_count": 4,
            }
        ],
        config=config,
    )

    assert "at least 5" in result["outcome"]["memory_candidates"]["rejected"][0]["reason"]
