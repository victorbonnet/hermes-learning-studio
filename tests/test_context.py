"""Context precedence, corrections, and the temporary/durable boundary.

The behaviours pinned here are the ones a learner would actually notice going
wrong: being told what they wanted last month instead of what they just
asked for, a passing remark hardening into a curriculum, or a correction that
does not stick.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from learning_studio import service
from learning_studio.config import LearningStudioConfig
from learning_studio.context import Candidate, resolve
from learning_studio.identity import Principal
from learning_studio.models import Provenance

LEARNER = Principal(
    profile="default", platform="telegram", user_id="3003", source="gateway_session"
)


# ── The precedence order itself ───────────────────────────────────────────


def _candidate(provenance: Provenance, value: str, **kwargs) -> Candidate:
    return Candidate(
        field="goal", value=value, provenance=provenance, source=provenance.value, **kwargs
    )


def test_precedence_order_is_the_documented_one():
    assert [p.value for p in Provenance] == [
        "explicit_request",
        "explicit_correction",
        "confirmed_track",
        "profile_config",
        "confirmed_preference",
        "recent_evidence",
        "default",
        "unconfirmed_inference",
    ]


def test_the_current_request_outranks_every_stored_source():
    """Rank 0 belongs to the request being made now, and only to it."""
    for stored in Provenance:
        resolved = resolve(
            [
                _candidate(stored, "stored", recorded_at="2030-01-01T00:00:00Z"),
                _candidate(Provenance.EXPLICIT_REQUEST, "said now", is_current=True),
            ]
        )
        assert resolved["goal"].value == "said now", f"{stored.value} outranked the live request"


def test_a_stored_explicit_statement_is_demoted_below_a_later_correction():
    """A saved remark is last session's; a correction came after it.

    Storing an explicit statement at rank 0 would let a stale value outrank
    the correction that exists precisely to supersede it.
    """
    resolved = resolve(
        [
            _candidate(Provenance.EXPLICIT_REQUEST, "what they said before"),
            _candidate(Provenance.EXPLICIT_CORRECTION, "what they corrected it to"),
        ]
    )

    assert resolved["goal"].value == "what they corrected it to"


def test_a_stored_explicit_statement_still_outranks_inference():
    resolved = resolve(
        [
            _candidate(Provenance.EXPLICIT_REQUEST, "they said this"),
            _candidate(Provenance.RECENT_EVIDENCE, "we guessed this"),
        ]
    )

    assert resolved["goal"].value == "they said this"


@pytest.mark.parametrize(
    ("winner", "loser"),
    [
        (Provenance.EXPLICIT_CORRECTION, Provenance.CONFIRMED_TRACK),
        (Provenance.CONFIRMED_TRACK, Provenance.PROFILE_CONFIG),
        (Provenance.PROFILE_CONFIG, Provenance.CONFIRMED_PREFERENCE),
        (Provenance.CONFIRMED_PREFERENCE, Provenance.RECENT_EVIDENCE),
        (Provenance.RECENT_EVIDENCE, Provenance.DEFAULT),
        (Provenance.DEFAULT, Provenance.UNCONFIRMED_INFERENCE),
    ],
)
def test_each_rank_beats_the_next(winner: Provenance, loser: Provenance):
    resolved = resolve([_candidate(loser, "loses"), _candidate(winner, "wins")])

    assert resolved["goal"].value == "wins"
    assert resolved["goal"].provenance is winner


def test_resolution_is_deterministic_regardless_of_input_order():
    pool = [
        _candidate(Provenance.RECENT_EVIDENCE, "evidence", recorded_at="2026-01-01T00:00:00Z"),
        _candidate(Provenance.CONFIRMED_TRACK, "track", recorded_at="2026-01-02T00:00:00Z"),
        _candidate(Provenance.DEFAULT, "default"),
    ]

    first = resolve(pool)["goal"]
    second = resolve(list(reversed(pool)))["goal"]

    assert first.value == second.value == "track"
    assert [s["value"] for s in first.superseded] == [s["value"] for s in second.superseded]


def test_a_live_statement_beats_an_identically_ranked_stored_one():
    """A stored explicit request is stale by definition; the current one is not."""
    resolved = resolve(
        [
            _candidate(
                Provenance.EXPLICIT_REQUEST, "stored last week", recorded_at="2026-01-01T00:00:00Z"
            ),
            _candidate(Provenance.EXPLICIT_REQUEST, "said just now", is_current=True),
        ]
    )

    assert resolved["goal"].value == "said just now"


def test_the_winner_reports_what_it_superseded():
    resolved = resolve(
        [
            _candidate(Provenance.CONFIRMED_TRACK, "track goal"),
            _candidate(Provenance.RECENT_EVIDENCE, "inferred goal"),
        ]
    )

    superseded = resolved["goal"].superseded
    assert [s["value"] for s in superseded] == ["inferred goal"]
    assert superseded[0]["provenance"] == "recent_evidence"


def test_only_confirmed_provenances_are_reported_as_confirmed():
    assert resolve([_candidate(Provenance.CONFIRMED_TRACK, "x")])["goal"].confirmed is True
    assert resolve([_candidate(Provenance.RECENT_EVIDENCE, "x")])["goal"].confirmed is False
    assert resolve([_candidate(Provenance.UNCONFIRMED_INFERENCE, "x")])["goal"].confirmed is False


# ── Precedence end to end ─────────────────────────────────────────────────


def test_the_current_request_overrides_a_stale_confirmed_track(hermes_home: Path):
    """The single most important rule: do not argue with someone about what they just said."""
    service.save_context(
        principal=LEARNER,
        track={
            "name": "Long-running track",
            "confirmed": True,
            "context": {"goal": "read unaided", "session_duration": "45 minutes"},
        },
    )

    result = service.get_context(
        principal=LEARNER, current_request={"goal": "pass the exam next week"}
    )

    goal = result["resolved_context"]["goal"]
    assert goal["value"] == "pass the exam next week"
    assert goal["provenance"] == "explicit_request"
    # The track value is not lost — it is reported as superseded.
    assert goal["superseded"][0]["value"] == "read unaided"
    # And a field the request did not mention still comes from the track.
    assert result["resolved_context"]["session_duration"]["provenance"] == "confirmed_track"


def test_a_confirmed_track_beats_temporary_evidence(hermes_home: Path):
    service.save_context(
        principal=LEARNER,
        track={"name": "T", "confirmed": True, "context": {"current_level": "intermediate"}},
    )
    service.save_context(principal=LEARNER, evidence_context={"current_level": "beginner"})

    result = service.get_context(principal=LEARNER)

    assert result["resolved_context"]["current_level"]["value"] == "intermediate"
    assert result["resolved_context"]["current_level"]["provenance"] == "confirmed_track"


def test_evidence_does_not_rewrite_a_confirmed_preference(hermes_home: Path):
    """Evidence adapts the temporary picture; it never edits what was confirmed."""
    service.save_context(
        principal=LEARNER,
        track={"name": "T", "confirmed": True, "context": {"feedback_preferences": ["blunt"]}},
    )
    service.save_context(principal=LEARNER, evidence_context={"feedback_preferences": ["gentle"]})

    track_id = service.get_context(principal=LEARNER)["track_selection"]["track_id"]
    confirmed = service.get_context(principal=LEARNER, track_id=track_id)["confirmed_context"]

    assert confirmed["feedback_preferences"]["value"] == ["blunt"]


def test_defaults_never_overwrite_a_stored_value(hermes_home: Path):
    config = LearningStudioConfig.from_mapping(
        {"learning_studio": {"defaults": {"session_duration": "20 minutes"}}}
    )
    service.save_context(
        principal=LEARNER, temporary_context={"session_duration": "90 minutes"}, config=config
    )

    result = service.get_context(principal=LEARNER, config=config)

    assert result["resolved_context"]["session_duration"]["value"] == "90 minutes"


def test_defaults_do_fill_a_gap(hermes_home: Path):
    config = LearningStudioConfig.from_mapping(
        {"learning_studio": {"defaults": {"session_duration": "20 minutes"}}}
    )

    result = service.get_context(principal=LEARNER, config=config)

    assert result["resolved_context"]["session_duration"]["value"] == "20 minutes"
    assert result["resolved_context"]["session_duration"]["provenance"] == "default"


def test_profile_config_outranks_a_stored_preference_but_not_the_request(hermes_home: Path):
    config = LearningStudioConfig.from_mapping(
        {"learning_studio": {"profile_context": {"explanation_language": "English"}}}
    )
    service.save_context(
        principal=LEARNER, evidence_context={"explanation_language": "French"}, config=config
    )

    stored = service.get_context(principal=LEARNER, config=config)
    assert stored["resolved_context"]["explanation_language"]["value"] == "English"

    asked = service.get_context(
        principal=LEARNER, current_request={"explanation_language": "Japanese"}, config=config
    )
    assert asked["resolved_context"]["explanation_language"]["value"] == "Japanese"


# ── Corrections ───────────────────────────────────────────────────────────


def test_a_correction_supersedes_the_value_it_corrects(hermes_home: Path):
    service.save_context(principal=LEARNER, temporary_context={"target_level": "fluent"})
    service.save_context(
        principal=LEARNER,
        corrections=[{"field": "target_level", "value": "conversational"}],
    )

    result = service.get_context(principal=LEARNER)

    assert result["resolved_context"]["target_level"]["value"] == "conversational"
    assert result["resolved_context"]["target_level"]["provenance"] == "explicit_correction"


def test_a_correction_preserves_the_value_it_replaced_as_a_revision(hermes_home: Path):
    from learning_studio import storage

    service.save_context(principal=LEARNER, temporary_context={"target_level": "fluent"})
    service.save_context(
        principal=LEARNER, corrections=[{"field": "target_level", "value": "conversational"}]
    )

    with storage.connect() as conn:
        rows = list(
            conn.execute(
                "SELECT * FROM context_revisions WHERE field = 'target_level'"
                " ORDER BY created_at, id"
            )
        )

    assert [r["change_reason"] for r in rows] == ["explicit_request", "explicit_correction"]
    assert rows[-1]["previous_value"] == "fluent"
    assert rows[-1]["new_value"] == "conversational"


def test_a_durable_correction_reaches_the_track(hermes_home: Path):
    created = service.save_context(
        principal=LEARNER,
        track={"name": "T", "confirmed": True, "context": {"goal": "original"}},
    )
    track_id = created["outcome"]["track"]["track_id"]

    service.save_context(
        principal=LEARNER,
        corrections=[
            {"field": "goal", "value": "corrected", "track_id": track_id, "durable": True}
        ],
    )

    result = service.get_context(principal=LEARNER, track_id=track_id)
    assert result["confirmed_context"]["goal"]["value"] == "corrected"


def test_a_correction_without_a_track_stays_temporary(hermes_home: Path):
    service.save_context(
        principal=LEARNER,
        track={"name": "T", "confirmed": True, "context": {"goal": "durable goal"}},
    )
    result = service.save_context(
        principal=LEARNER, corrections=[{"field": "goal", "value": "just for now"}]
    )

    assert result["outcome"]["corrections"][0]["durable"] is False
    track_id = service.get_context(principal=LEARNER)["track_selection"]["track_id"]
    confirmed = service.get_context(principal=LEARNER, track_id=track_id)["confirmed_context"]
    assert confirmed["goal"]["value"] == "durable goal"


# ── Temporary versus confirmed ────────────────────────────────────────────


def test_temporary_and_confirmed_context_are_returned_separately(hermes_home: Path):
    service.save_context(
        principal=LEARNER,
        track={"name": "T", "confirmed": True, "context": {"goal": "durable"}},
    )
    service.save_context(principal=LEARNER, temporary_context={"subject": "provisional"})

    result = service.get_context(principal=LEARNER)

    assert "goal" in result["confirmed_context"]
    assert "goal" not in result["temporary_context"]
    assert "subject" in result["temporary_context"]
    assert "subject" not in result["confirmed_context"]


def test_temporary_context_is_marked_unconfirmed(hermes_home: Path):
    service.save_context(principal=LEARNER, evidence_context={"current_level": "beginner"})

    result = service.get_context(principal=LEARNER)

    assert result["temporary_context"]["current_level"]["confirmed"] is False


def test_a_one_off_request_does_not_become_a_track(hermes_home: Path):
    """ "Quiz me on this chapter" is not a curriculum."""
    result = service.save_context(
        principal=LEARNER, temporary_context={"subject": "chapter 4", "goal": "quiz me"}
    )

    assert result["outcome"]["track"]["status"] == "not_requested"
    assert service.get_context(principal=LEARNER)["tracks"] == []


def test_creating_a_track_without_confirmation_is_refused(hermes_home: Path):
    result = service.save_context(
        principal=LEARNER, track={"name": "Presumed track", "context": {"goal": "g"}}
    )

    assert result["outcome"]["track"]["status"] == "rejected"
    assert "explicit learner confirmation" in result["outcome"]["track"]["reason"]
    assert service.get_context(principal=LEARNER)["tracks"] == []


def test_confirmed_false_is_not_confirmation(hermes_home: Path):
    result = service.save_context(
        principal=LEARNER, track={"name": "T", "confirmed": False, "context": {"goal": "g"}}
    )

    assert result["outcome"]["track"]["status"] == "rejected"
    assert service.get_context(principal=LEARNER)["tracks"] == []


@pytest.mark.parametrize("truthy", ["true", 1, "yes", [1], {"a": 1}])
def test_only_a_real_boolean_true_counts_as_confirmation(hermes_home: Path, truthy):
    """A truthy string from a sloppy caller must not create a durable record."""
    result = service.save_context(
        principal=LEARNER, track={"name": f"T{truthy}", "confirmed": truthy}
    )

    assert result["outcome"]["track"]["status"] == "rejected"


def test_creating_a_track_with_explicit_confirmation_succeeds(hermes_home: Path):
    result = service.save_context(
        principal=LEARNER,
        track={"name": "Confirmed track", "confirmed": True, "context": {"goal": "g"}},
    )

    assert result["outcome"]["track"]["status"] == "created"
    assert service.get_context(principal=LEARNER)["tracks"][0]["name"] == "Confirmed track"


def test_repeated_sessions_do_not_add_up_to_confirmation(hermes_home: Path):
    for _ in range(5):
        service.save_context(principal=LEARNER, temporary_context={"subject": "same thing"})

    assert service.get_context(principal=LEARNER)["tracks"] == []


def test_a_rejected_track_still_keeps_the_context_as_temporary(hermes_home: Path):
    service.save_context(
        principal=LEARNER,
        temporary_context={"subject": "kept"},
        track={"name": "Unconfirmed", "context": {"goal": "dropped"}},
    )

    result = service.get_context(principal=LEARNER)

    assert result["temporary_context"]["subject"]["value"] == "kept"
    assert result["tracks"] == []


# ── Expiry ────────────────────────────────────────────────────────────────


def test_temporary_context_expires(hermes_home: Path):
    from learning_studio import storage

    service.save_context(principal=LEARNER, temporary_context={"subject": "ephemeral"})

    with storage.connect() as conn:
        conn.execute(
            "UPDATE learning_contexts SET expires_at = '2000-01-01T00:00:00+00:00'"
            " WHERE scope = 'temporary'"
        )
        conn.commit()

    result = service.get_context(principal=LEARNER)
    assert result["temporary_context"] == {}


def test_expiring_temporary_context_leaves_confirmed_tracks_alone(hermes_home: Path):
    from learning_studio import storage

    service.save_context(
        principal=LEARNER,
        track={"name": "Durable", "confirmed": True, "context": {"goal": "survives"}},
        temporary_context={"subject": "ephemeral"},
    )

    with storage.connect() as conn:
        conn.execute(
            "UPDATE learning_contexts SET expires_at = '2000-01-01T00:00:00+00:00'"
            " WHERE scope = 'temporary'"
        )
        conn.commit()

    result = service.get_context(principal=LEARNER)
    assert result["temporary_context"] == {}
    assert result["confirmed_context"]["goal"]["value"] == "survives"


# ── Objectives ────────────────────────────────────────────────────────────


def test_an_objective_records_behavior_condition_and_standard(hermes_home: Path):
    created = service.save_context(principal=LEARNER, track={"name": "T", "confirmed": True})
    track_id = created["outcome"]["track"]["track_id"]

    service.save_context(
        principal=LEARNER,
        objectives=[
            {
                "track_id": track_id,
                "behavior": "state the base case",
                "condition": "given a recursive function, unaided",
                "standard": "4 times in 5",
            }
        ],
    )

    objectives = service.get_context(principal=LEARNER, track_id=track_id)["objectives"]
    assert objectives[0]["behavior"] == "state the base case"
    assert objectives[0]["condition"] == "given a recursive function, unaided"
    assert objectives[0]["standard"] == "4 times in 5"
    assert objectives[0]["status"] == "active"


def test_an_objective_is_not_marked_met_without_confirmation(hermes_home: Path):
    """One right answer is not the same as meeting a standard."""
    created = service.save_context(principal=LEARNER, track={"name": "T", "confirmed": True})
    track_id = created["outcome"]["track"]["track_id"]

    result = service.save_context(
        principal=LEARNER,
        objectives=[
            {
                "track_id": track_id,
                "behavior": "b",
                "condition": "c",
                "standard": "s",
                "status": "met",
            }
        ],
    )

    assert result["outcome"]["objectives"][0]["status"] == "rejected"
    assert service.get_context(principal=LEARNER, track_id=track_id)["objectives"] == []


def test_an_objective_may_be_marked_met_with_confirmation(hermes_home: Path):
    created = service.save_context(principal=LEARNER, track={"name": "T", "confirmed": True})
    track_id = created["outcome"]["track"]["track_id"]

    service.save_context(
        principal=LEARNER,
        objectives=[
            {
                "track_id": track_id,
                "behavior": "b",
                "condition": "c",
                "standard": "s",
                "status": "met",
                "confirm_met": True,
            }
        ],
    )

    objectives = service.get_context(principal=LEARNER, track_id=track_id)["objectives"]
    assert objectives[0]["status"] == "met"


def test_an_objective_needs_a_track(hermes_home: Path):
    result = service.save_context(
        principal=LEARNER,
        objectives=[{"behavior": "b", "condition": "c", "standard": "s"}],
    )

    assert result["outcome"]["objectives"][0]["status"] == "rejected"
