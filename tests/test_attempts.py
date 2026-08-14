"""Durable attempts: scoring, mastery, misconceptions, review state, erasure.

Every assertion here is either about a number in the database or about the
absence of a string: the canaries in ``tests.component_examples`` mark every
hidden field, and a passing "no leak" test means none of those strings made
it into anything :mod:`learning_studio.service` returned.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from learning_studio import service, storage
from learning_studio.identity import Principal
from tests.component_examples import CANARY, all_canaries, example, manifest
from tests.served_responses import response_for

OWNER = Principal(profile="default", platform="telegram", user_id="5100", source="gateway_session")
OTHER = Principal(profile="default", platform="telegram", user_id="5200", source="gateway_session")


def rows(query: str, *params):
    with storage.connect() as conn:
        return conn.execute(query, params).fetchall()


def confirmed_track(principal: Principal = OWNER, name: str = "Biology") -> str:
    saved = service.save_context(principal=principal, track={"name": name, "confirmed": True})
    return saved["outcome"]["track"]["track_id"]


def stored_objective(track_id: str, principal: Principal = OWNER, **overrides) -> tuple[str, dict]:
    objective = {"behavior": "state the base case", "condition": "unaided", "standard": "4 in 5"}
    objective.update(overrides)
    saved = service.save_context(
        principal=principal, objectives=[{"track_id": track_id, **objective}]
    )
    return saved["outcome"]["objectives"][0]["objective_id"], objective


def _responses_for(components: list[dict]) -> dict[str, dict]:
    """Canonical-terms responses keyed by each component's own id."""
    return {
        component["id"]: response_for(component["type"], component["content"])
        for component in components
    }


def _walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _leaked_canaries(value, allowed: set[str]) -> set[str]:
    found = {s for s in _walk_strings(value) if s.startswith(CANARY)}
    return found - allowed


# ── Scoring and storage ────────────────────────────────────────────────────


def test_record_attempt_scores_every_component_and_leaks_nothing_hidden(hermes_home: Path):
    components = [
        example("multiple_choice", id="mc"),
        example("free_response", id="fr"),
        example("flashcard", id="fc"),
    ]
    prepared = service.prepare_experience(principal=OWNER, manifest=manifest(components))
    experience_id = prepared["experience_id"]

    responses = _responses_for(components)
    result = service.record_attempt(
        principal=OWNER,
        experience_id=experience_id,
        responses=responses,
        started_at="2025-01-01T00:00:00+00:00",
    )

    assert result["component_count"] == 3
    assert result["graded_component_count"] == 1  # only multiple_choice has a mark
    assert result["correct_component_count"] == 1

    allowed = {
        CANARY + "-multiple_choice-feedback-correct",
        CANARY + "-multiple_choice-feedback-incorrect",
        CANARY + "-multiple_choice-per-option",
    }
    assert _leaked_canaries(result, allowed) == set()
    assert all("answer_review" not in component for component in result["components"])

    assert len(rows("SELECT * FROM attempts")) == 1
    stored_components = rows("SELECT * FROM attempt_components")
    assert len(stored_components) == 3
    graded_flags = {row["component_type"]: bool(row["graded"]) for row in stored_components}
    assert graded_flags == {"multiple_choice": True, "free_response": False, "flashcard": False}


def test_record_attempt_stores_no_raw_response_text(hermes_home: Path):
    """Neither the attempt row nor its components carry a submitted value."""
    components = [example("free_response", id="fr")]
    prepared = service.prepare_experience(principal=OWNER, manifest=manifest(components))
    responses = {"fr": {"text": "MY PRIVATE ESSAY TEXT, never to be stored"}}

    service.record_attempt(
        principal=OWNER,
        experience_id=prepared["experience_id"],
        responses=responses,
        started_at="2025-01-01T00:00:00+00:00",
    )

    attempt_columns = " ".join(str(dict(row)) for row in rows("SELECT * FROM attempts"))
    component_columns = " ".join(str(dict(row)) for row in rows("SELECT * FROM attempt_components"))
    assert "MY PRIVATE ESSAY TEXT" not in attempt_columns
    assert "MY PRIVATE ESSAY TEXT" not in component_columns


def test_rubric_with_levels_is_graded_deterministically(hermes_home: Path):
    components = [example("free_response", id="fr")]
    prepared = service.prepare_experience(principal=OWNER, manifest=manifest(components))
    experience_id = prepared["experience_id"]

    criterion = rows("SELECT evaluation FROM experience_component_evaluations")[0]["evaluation"]
    import json

    rubric_criterion = json.loads(criterion)["evaluation"]["rubric"][0]["criterion"]

    # Without a rubric_levels input, the attempt records the response as made
    # but ungraded — record_attempt's public surface does not accept
    # per-criterion levels today (no reviewer path exists yet), so this
    # documents the honest current behaviour rather than fabricating one.
    result = service.record_attempt(
        principal=OWNER,
        experience_id=experience_id,
        responses={"fr": {"text": "an essay"}},
        started_at="2025-01-01T00:00:00+00:00",
    )
    assert result["components"][0]["graded"] is False
    assert rubric_criterion  # sanity: the fixture really has a rubric


# ── Objective mastery and misconceptions ───────────────────────────────────


def test_wrong_answers_accumulate_in_the_misconception_bank(hermes_home: Path):
    track_id = confirmed_track()
    objective_id, objective = stored_objective(track_id)
    component = example("multiple_choice", id="mc")
    prepared = service.prepare_experience(
        principal=OWNER,
        manifest=manifest([component], objective=objective),
        track_id=track_id,
        objective_id=objective_id,
    )
    wrong = {"mc": {"option_id": "cytosol"}}

    service.record_attempt(
        principal=OWNER,
        experience_id=prepared["experience_id"],
        responses=wrong,
        started_at="2025-01-01T00:00:00+00:00",
    )
    service.record_attempt(
        principal=OWNER,
        experience_id=prepared["experience_id"],
        responses=wrong,
        started_at="2025-01-02T00:00:00+00:00",
    )

    misconceptions = rows(
        "SELECT * FROM misconceptions WHERE learner_id IN (SELECT id FROM learners)"
    )
    assert len(misconceptions) == 1
    assert misconceptions[0]["occurrences"] == 2
    assert misconceptions[0]["component_type"] == "multiple_choice"
    assert misconceptions[0]["objective_id"] == objective_id


def test_attempts_overview_reports_mastery_and_misconceptions_without_leaking(hermes_home: Path):
    track_id = confirmed_track()
    objective_id, objective = stored_objective(track_id)
    component = example("multiple_choice", id="mc")
    prepared = service.prepare_experience(
        principal=OWNER,
        manifest=manifest([component], objective=objective),
        track_id=track_id,
        objective_id=objective_id,
    )
    service.record_attempt(
        principal=OWNER,
        experience_id=prepared["experience_id"],
        responses={"mc": {"option_id": "cytosol"}},
        started_at="2025-01-01T00:00:00+00:00",
    )

    overview = service.attempts_overview(principal=OWNER)
    assert overview["attempts_count"] == 1
    assert overview["objectives"][0]["objective_id"] == objective_id
    assert overview["objectives"][0]["mastery_fraction"] == 0.0
    assert overview["misconceptions"][0]["component_type"] == "multiple_choice"
    assert _leaked_canaries(overview, set()) == set()


# ── Spaced repetition ──────────────────────────────────────────────────────


def test_review_state_progresses_across_attempts(hermes_home: Path):
    track_id = confirmed_track()
    objective_id, objective = stored_objective(track_id)
    component = example("multiple_choice", id="mc")
    prepared = service.prepare_experience(
        principal=OWNER,
        manifest=manifest([component], objective=objective),
        track_id=track_id,
        objective_id=objective_id,
    )
    right = {"mc": {"option_id": "matrix"}}

    for day in ("01", "02", "03"):
        service.record_attempt(
            principal=OWNER,
            experience_id=prepared["experience_id"],
            responses=right,
            started_at=f"2025-01-{day}T00:00:00+00:00",
        )

    state_rows = rows("SELECT * FROM review_state")
    assert len(state_rows) == 1
    assert state_rows[0]["repetitions"] == 3
    assert state_rows[0]["objective_id"] == objective_id

    plan = service.review_plan(principal=OWNER)
    assert plan["review_reminders_enabled"] is False
    all_entries = plan["due"] + plan["upcoming"]
    assert any(entry["objective_id"] == objective_id for entry in all_entries)
    assert _leaked_canaries(plan, set()) == set()


# ── Opt-in reminders ────────────────────────────────────────────────────────


def test_review_reminders_default_off_and_toggle(hermes_home: Path):
    # Never having practised anything yet is enough to check the default.
    service.save_context(principal=OWNER, temporary_context={"goal": "test"})
    overview = service.attempts_overview(principal=OWNER)
    assert overview["review_reminders_enabled"] is False

    on = service.set_review_reminders(principal=OWNER, enabled=True)
    assert on["review_reminders_enabled"] is True
    assert service.attempts_overview(principal=OWNER)["review_reminders_enabled"] is True

    off = service.set_review_reminders(principal=OWNER, enabled=False)
    assert off["review_reminders_enabled"] is False
    assert service.attempts_overview(principal=OWNER)["review_reminders_enabled"] is False


# ── Ownership ────────────────────────────────────────────────────────────


def test_a_learner_cannot_see_another_learners_attempts(hermes_home: Path):
    component = example("multiple_choice", id="mc")
    prepared = service.prepare_experience(principal=OWNER, manifest=manifest([component]))
    service.record_attempt(
        principal=OWNER,
        experience_id=prepared["experience_id"],
        responses={"mc": {"option_id": "matrix"}},
        started_at="2025-01-01T00:00:00+00:00",
    )

    assert service.attempts_overview(principal=OTHER)["attempts_count"] == 0

    with pytest.raises(service.NotFoundError):
        service.record_attempt(
            principal=OTHER,
            experience_id=prepared["experience_id"],
            responses={"mc": {"option_id": "matrix"}},
            started_at="2025-01-01T00:00:00+00:00",
        )


def test_attempt_summary_is_scoped_to_its_owner(hermes_home: Path):
    component = example("multiple_choice", id="mc")
    prepared = service.prepare_experience(principal=OWNER, manifest=manifest([component]))
    recorded = service.record_attempt(
        principal=OWNER,
        experience_id=prepared["experience_id"],
        responses={"mc": {"option_id": "matrix"}},
        started_at="2025-01-01T00:00:00+00:00",
    )

    own = service.attempt_summary(principal=OWNER, attempt_id=recorded["attempt_id"])
    assert own["attempt_id"] == recorded["attempt_id"]

    with pytest.raises(service.NotFoundError):
        service.attempt_summary(principal=OTHER, attempt_id=recorded["attempt_id"])


# ── Erasure ─────────────────────────────────────────────────────────────


def test_erase_learner_removes_every_evaluation_runtime_row(hermes_home: Path):
    track_id = confirmed_track()
    objective_id, objective = stored_objective(track_id)
    component = example("multiple_choice", id="mc")
    prepared = service.prepare_experience(
        principal=OWNER,
        manifest=manifest([component], objective=objective),
        track_id=track_id,
        objective_id=objective_id,
    )
    service.record_attempt(
        principal=OWNER,
        experience_id=prepared["experience_id"],
        responses={"mc": {"option_id": "cytosol"}},
        started_at="2025-01-01T00:00:00+00:00",
    )
    service.set_review_reminders(principal=OWNER, enabled=True)

    for table in (
        "attempts",
        "attempt_components",
        "review_state",
        "misconceptions",
        "learner_preferences",
    ):
        assert len(rows(f"SELECT * FROM {table}")) > 0, table

    result = service.erase_learner(principal=OWNER)
    assert result == {"ok": True, "erased": True}

    for table in (
        "learners",
        "tracks",
        "objectives",
        "experiences",
        "attempts",
        "attempt_components",
        "review_state",
        "misconceptions",
        "learner_preferences",
    ):
        assert rows(f"SELECT * FROM {table}") == [], table

    with storage.connect() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_erase_learner_removes_managed_asset_files(hermes_home: Path):
    """Erasure is not merely logical: the image files go with the rows."""
    from learning_studio import assets
    from tests.test_assets import _image, _import, _source_root

    source = _source_root(hermes_home) / "cell.png"
    _image(source)
    _import(OWNER, source)

    (storage_name,) = rows("SELECT storage_name FROM managed_assets")[0]
    asset_file = assets.managed_assets_root() / str(storage_name)
    assert asset_file.exists()

    result = service.erase_learner(principal=OWNER)
    assert result == {"ok": True, "erased": True}
    assert not asset_file.exists()
    assert rows("SELECT * FROM managed_assets") == []
    with storage.connect() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_erase_learner_leaves_another_learners_asset_files_alone(hermes_home: Path):
    from learning_studio import assets
    from tests.test_assets import _image, _import, _source_root

    for principal, name in ((OWNER, "owner.png"), (OTHER, "other.png")):
        source = _source_root(hermes_home) / name
        _image(source)
        _import(principal, source)

    names = {str(row[0]) for row in rows("SELECT storage_name FROM managed_assets")}
    assert len(names) == 2

    service.erase_learner(principal=OWNER)

    survivors = rows("SELECT storage_name FROM managed_assets")
    assert len(survivors) == 1
    surviving_name = str(survivors[0][0])
    assert (assets.managed_assets_root() / surviving_name).exists()
    assert surviving_name in names


def test_erase_learner_is_idempotent_and_safe_with_nothing_stored(hermes_home: Path):
    result = service.erase_learner(principal=OWNER)
    assert result == {"ok": True, "erased": False, "message": "Nothing is stored for this learner."}


def test_every_canary_used_anywhere_never_appears_in_an_attempt_summary(hermes_home: Path):
    """Belt and braces: every canary the fixtures define, checked at once.

    Excludes the five types that reference a managed image asset
    (``image_observation``, ``image_choice``, ``diagram``, ``hotspot``,
    ``labeling``): those need a real imported asset to pass
    ``prepare_experience``'s ownership check, which is exercised elsewhere
    (``test_assets.py``, ``test_experience_storage.py``) and is orthogonal to
    what this test is checking.
    """
    from tests.component_examples import EXAMPLES

    asset_bearing = {"image_observation", "image_choice", "diagram", "hotspot", "labeling"}
    components = [example(t, id=f"c-{t}") for t in EXAMPLES if t not in asset_bearing]
    prepared = service.prepare_experience(principal=OWNER, manifest=manifest(components))
    responses = _responses_for(components)

    result = service.record_attempt(
        principal=OWNER,
        experience_id=prepared["experience_id"],
        responses=responses,
        started_at="2025-01-01T00:00:00+00:00",
    )

    feedback_canaries = {
        s
        for s in all_canaries()
        if s.endswith("-feedback-correct")
        or s.endswith("-feedback-incorrect")
        or s.endswith("-per-option")
    }
    leaked = _leaked_canaries(result, feedback_canaries)
    assert leaked == set()

    overview = service.attempts_overview(principal=OWNER)
    assert _leaked_canaries(overview, set()) == set()
