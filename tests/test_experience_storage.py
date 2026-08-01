"""Storing an experience: transactions, ownership, and deterministic order.

Every test takes the ``hermes_home`` fixture, so nothing here can reach a real
profile. The assertions are about the two guarantees a later runtime will
depend on without being able to re-check them: that a stored experience
belongs to exactly one learner in one profile, and that the learner-facing
table holds nothing an evaluator wrote.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from learning_studio import service, storage
from learning_studio.identity import Principal
from tests.component_examples import CANARY, example, manifest

OWNER = Principal(profile="default", platform="telegram", user_id="4100", source="gateway_session")
OTHER = Principal(profile="default", platform="telegram", user_id="4200", source="gateway_session")
#: The same numeric id on another platform. A different person, always.
SAME_ID_ELSEWHERE = Principal(
    profile="default", platform="discord", user_id="4100", source="gateway_session"
)
OTHER_PROFILE = Principal(
    profile="work", platform="telegram", user_id="4100", source="gateway_session"
)


def prepare(principal: Principal = OWNER, **kwargs):
    return service.prepare_experience(
        principal=principal, manifest=kwargs.pop("manifest", manifest()), **kwargs
    )


def confirmed_track(principal: Principal = OWNER, name: str = "Biology") -> str:
    saved = service.save_context(
        principal=principal,
        track={"name": name, "confirmed": True, "context": {"goal": "pass the exam"}},
    )
    return saved["outcome"]["track"]["track_id"]


def rows(query: str, *params):
    with storage.connect() as conn:
        return conn.execute(query, params).fetchall()


# ── The happy path ────────────────────────────────────────────────────────


def test_preparing_stores_one_experience(hermes_home: Path):
    result = prepare()

    assert result["ok"] is True
    assert result["stored"] is True
    assert len(rows("SELECT * FROM experiences")) == 1


def test_the_experience_id_is_opaque(hermes_home: Path):
    """Not a title, not a slug, not anything a caller chose."""
    result = prepare(manifest=manifest(title="Photosynthesis revision"))

    experience_id = result["experience_id"]
    assert len(experience_id) == 32
    assert experience_id.isalnum()
    assert "photosynthesis" not in experience_id.lower()


def test_two_experiences_get_different_ids(hermes_home: Path):
    first = prepare()["experience_id"]
    second = prepare()["experience_id"]

    assert first != second


def test_every_stored_row_carries_its_owner(hermes_home: Path):
    prepare()

    experience = rows("SELECT * FROM experiences")[0]
    component = rows("SELECT * FROM experience_components")[0]

    assert experience["profile_id"] == "default"
    assert experience["learner_id"] == component["learner_id"]
    assert component["profile_id"] == "default"


def test_no_raw_platform_identifier_is_stored(hermes_home: Path):
    """The learner column is a generated id, never the platform's user id.

    The principal here has a full-length platform id rather than the short one
    the other tests use. The database is full of hex — a salt, a digest, and a
    generated id per row — and a four-digit needle turns up in random hex often
    enough to make this assertion flaky rather than meaningful. A ten-digit
    one, which is what a real platform id looks like anyway, does not.
    """
    long_id = "4100077231"
    principal = Principal(
        profile="default", platform="telegram", user_id=long_id, source="gateway_session"
    )

    prepare(principal)

    with storage.connect() as conn:
        dump = "".join(line for line in conn.iterdump())

    assert long_id not in dump


# ── Component ordering ────────────────────────────────────────────────────


def test_components_are_stored_in_manifest_order(hermes_home: Path):
    components = [
        example("true_false", id="first"),
        example("short_answer", id="second"),
        example("flashcard", id="third"),
    ]

    prepare(manifest=manifest(components))

    stored = rows("SELECT position, component_key FROM experience_components ORDER BY position")
    assert [(row["position"], row["component_key"]) for row in stored] == [
        (1, "first"),
        (2, "second"),
        (3, "third"),
    ]


def test_the_stored_order_is_deterministic_across_reads(hermes_home: Path):
    components = [example("true_false", id=f"item-{index}") for index in range(8)]
    experience_id = prepare(manifest=manifest(components))["experience_id"]

    reads = [
        [
            entry["component_id"]
            for entry in service.get_experience(principal=OWNER, experience_id=experience_id)[
                "components"
            ]
        ]
        for _ in range(3)
    ]

    assert reads[0] == reads[1] == reads[2] == [f"item-{index}" for index in range(8)]


def test_the_author_component_id_is_not_a_primary_key(hermes_home: Path):
    """Two experiences may reuse an id; rows key on a generated one."""
    first = prepare()["experience_id"]
    second = prepare()["experience_id"]

    stored = rows("SELECT id, experience_id, component_key FROM experience_components")
    assert len({row["id"] for row in stored}) == 2
    assert {row["component_key"] for row in stored} == {"resp-01"}
    assert {row["experience_id"] for row in stored} == {first, second}


# ── Hidden data is in the other table ─────────────────────────────────────


def test_the_learner_payload_table_holds_no_evaluator_data(hermes_home: Path):
    prepare(manifest=manifest([example(name) for name in ("multiple_choice", "flashcard")]))

    payloads = " ".join(
        str(row["learner_payload"])
        for row in rows("SELECT learner_payload FROM experience_components")
    )

    assert CANARY not in payloads


def test_the_evaluation_table_holds_the_evaluator_data(hermes_home: Path):
    """The mirror: proves the assertion above is not passing vacuously."""
    prepare()

    stored = " ".join(
        str(row["evaluation"])
        for row in rows("SELECT evaluation FROM experience_component_evaluations")
    )

    assert CANARY in stored


def test_a_component_with_no_hidden_half_stores_no_evaluation_row(hermes_home: Path):
    payload = example("reflection")
    del payload["evaluation"]

    prepare(manifest=manifest([payload]))

    assert rows("SELECT * FROM experience_component_evaluations") == []


def test_the_read_projection_returns_only_learner_payloads(hermes_home: Path):
    experience_id = prepare()["experience_id"]

    stored = service.get_experience(principal=OWNER, experience_id=experience_id)

    assert CANARY not in json.dumps(stored)
    assert stored["components"][0]["payload"]["prompt"]


# ── Ownership ─────────────────────────────────────────────────────────────


def test_another_learner_cannot_read_an_experience(hermes_home: Path):
    experience_id = prepare(OWNER)["experience_id"]

    with pytest.raises(service.NotFoundError):
        service.get_experience(principal=OTHER, experience_id=experience_id)


def test_the_same_numeric_id_on_another_platform_cannot_read_it(hermes_home: Path):
    """Telegram 4100 and Discord 4100 are two people, and stay two people."""
    experience_id = prepare(OWNER)["experience_id"]

    with pytest.raises(service.NotFoundError):
        service.get_experience(principal=SAME_ID_ELSEWHERE, experience_id=experience_id)


def test_another_profile_cannot_read_an_experience(hermes_home: Path):
    experience_id = prepare(OWNER)["experience_id"]

    with pytest.raises(service.NotFoundError):
        service.get_experience(principal=OTHER_PROFILE, experience_id=experience_id)


def test_a_learner_with_no_record_gets_the_same_refusal(hermes_home: Path):
    """Not-found and not-yours must be indistinguishable."""
    experience_id = prepare(OWNER)["experience_id"]

    with pytest.raises(service.NotFoundError) as unknown_learner:
        service.get_experience(principal=OTHER, experience_id=experience_id)

    prepare(OTHER)
    with pytest.raises(service.NotFoundError) as known_learner:
        service.get_experience(principal=OTHER, experience_id=experience_id)

    assert str(unknown_learner.value) == str(known_learner.value)


def test_an_unknown_experience_id_is_refused(hermes_home: Path):
    prepare(OWNER)

    with pytest.raises(service.NotFoundError):
        service.get_experience(principal=OWNER, experience_id="0" * 32)


# ── Track and objective ownership ─────────────────────────────────────────


def test_an_experience_may_be_attached_to_a_confirmed_track(hermes_home: Path):
    track_id = confirmed_track()

    result = prepare(track_id=track_id)

    assert result["track_id"] == track_id
    assert rows("SELECT track_id FROM experiences")[0]["track_id"] == track_id


def test_another_learners_track_cannot_be_referenced(hermes_home: Path):
    track_id = confirmed_track(OWNER)

    with pytest.raises(service.NotFoundError):
        prepare(OTHER, track_id=track_id)


def test_a_cross_learner_attempt_stores_nothing(hermes_home: Path):
    track_id = confirmed_track(OWNER)

    with pytest.raises(service.NotFoundError):
        prepare(OTHER, track_id=track_id)

    assert rows("SELECT * FROM experiences") == []


def test_an_unknown_track_is_refused(hermes_home: Path):
    with pytest.raises(service.NotFoundError):
        prepare(track_id="0" * 32)


def test_an_archived_track_takes_no_new_experience(hermes_home: Path):
    """A learner who set a track aside has said they are done with it."""
    track_id = confirmed_track()
    service.save_context(principal=OWNER, track={"track_id": track_id, "status": "archived"})

    with pytest.raises(service.ValidationError, match="archived"):
        prepare(track_id=track_id)


def test_a_withdrawn_track_takes_no_new_experience(hermes_home: Path):
    track_id = confirmed_track()
    service.save_context(principal=OWNER, track={"track_id": track_id, "status": "withdrawn"})

    with pytest.raises(service.ValidationError, match="withdrawn"):
        prepare(track_id=track_id)


def stored_objective(track_id: str, principal: Principal = OWNER, **overrides) -> tuple[str, dict]:
    """An objective on *track_id*, and the manifest objective that matches it."""
    objective = {"behavior": "state the base case", "condition": "unaided", "standard": "4 in 5"}
    objective.update(overrides)
    saved = service.save_context(
        principal=principal, objectives=[{"track_id": track_id, **objective}]
    )
    return saved["outcome"]["objectives"][0]["objective_id"], objective


def test_an_objective_may_be_named_when_its_track_is(hermes_home: Path):
    track_id = confirmed_track()
    objective_id, objective = stored_objective(track_id)

    result = prepare(
        manifest=manifest(objective=objective), track_id=track_id, objective_id=objective_id
    )

    assert result["objective_id"] == objective_id


def test_an_objective_without_its_track_is_refused(hermes_home: Path):
    with pytest.raises(service.ValidationError, match="track_id"):
        prepare(objective_id="0" * 32)


def test_another_learners_objective_is_refused(hermes_home: Path):
    track_id = confirmed_track(OWNER)
    objective_id, objective = stored_objective(track_id)
    other_track = confirmed_track(OTHER)

    with pytest.raises(service.NotFoundError):
        prepare(
            OTHER,
            manifest=manifest(objective=objective),
            track_id=other_track,
            objective_id=objective_id,
        )


def test_a_retired_objective_is_refused(hermes_home: Path):
    track_id = confirmed_track()
    objective_id, _ = stored_objective(track_id)
    service.save_context(
        principal=OWNER,
        objectives=[{"objective_id": objective_id, "track_id": track_id, "status": "retired"}],
    )

    with pytest.raises(service.ValidationError, match="retired"):
        prepare(track_id=track_id, objective_id=objective_id)


# ── Ownership is a database constraint, not only a query convention ───────


def test_the_schema_refuses_an_experience_for_a_learner_that_does_not_exist(hermes_home: Path):
    prepare()

    with storage.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO experiences"
            " (id, learner_id, profile_id, manifest_schema_version, title,"
            "  objective_behavior, objective_condition, objective_standard, instructions,"
            "  ui_locale, expected_duration_minutes, difficulty, accessibility,"
            "  source_references, delivery, component_count, created_at, updated_at)"
            " VALUES ('x', 'nobody', 'default', 1, 't', 'b', 'c', 's', 'i', 'en', 5,"
            " 'introductory', '{}', '[]', '{}', 1, 'now', 'now')"
        )


def test_the_schema_refuses_a_component_whose_owner_differs_from_its_experience(
    hermes_home: Path,
):
    """The composite foreign key is what makes the denormalised columns safe."""
    prepare()
    experience = rows("SELECT * FROM experiences")[0]

    with storage.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO experience_components"
            " (id, experience_id, learner_id, profile_id, position, component_key,"
            "  component_type, learner_payload, created_at)"
            " VALUES ('c', ?, 'someone-else', ?, 9, 'k', 'true_false', '{}', 'now')",
            (experience["id"], experience["profile_id"]),
        )


def test_the_schema_refuses_two_components_at_the_same_position(hermes_home: Path):
    prepare()
    component = rows("SELECT * FROM experience_components")[0]

    with storage.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO experience_components"
            " (id, experience_id, learner_id, profile_id, position, component_key,"
            "  component_type, learner_payload, created_at)"
            " VALUES ('c2', ?, ?, ?, ?, 'other-key', 'true_false', '{}', 'now')",
            (
                component["experience_id"],
                component["learner_id"],
                component["profile_id"],
                component["position"],
            ),
        )


def test_deleting_a_learner_cascades_to_their_experiences(hermes_home: Path):
    prepare()

    with storage.connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM learners")
        conn.commit()

    assert rows("SELECT * FROM experiences") == []
    assert rows("SELECT * FROM experience_components") == []
    assert rows("SELECT * FROM experience_component_evaluations") == []


# ── Transactions ──────────────────────────────────────────────────────────


def test_an_invalid_manifest_writes_nothing_at_all(hermes_home: Path):
    """Validation runs before the database is opened, so there is no row *and*
    no file to hold one."""
    from learning_studio.paths import storage_root

    with pytest.raises(service.ValidationError):
        prepare(manifest=manifest(difficulty="nonsense"))

    assert not (storage_root() / "learning-studio.sqlite3").exists()


def test_a_failure_partway_through_the_components_rolls_the_whole_thing_back(
    hermes_home: Path, monkeypatch
):
    """The last-line payload check runs inside the transaction, so it can fail it.

    Simulated by making that check reject the third component. What matters is
    that a failure *after* the experience row and two component rows are
    written leaves none of them behind.
    """
    components = [example("true_false", id=f"item-{index}") for index in range(4)]
    calls: list[str] = []

    def explode(payload, component):
        calls.append(component.id)
        if component.id == "item-2":
            raise service.ValidationError("simulated failure on the third component")

    monkeypatch.setattr(service, "_assert_payload_is_safe", explode)

    with pytest.raises(service.ValidationError):
        prepare(manifest=manifest(components))

    assert calls == ["item-0", "item-1", "item-2"], "the failure did not happen partway through"
    assert rows("SELECT * FROM experiences") == []
    assert rows("SELECT * FROM experience_components") == []


def test_a_storage_failure_leaves_earlier_experiences_intact(hermes_home: Path, monkeypatch):
    first = prepare()["experience_id"]

    def explode(payload, component):
        raise service.ValidationError("simulated failure")

    monkeypatch.setattr(service, "_assert_payload_is_safe", explode)
    with pytest.raises(service.ValidationError):
        prepare()

    assert [row["id"] for row in rows("SELECT id FROM experiences")] == [first]


def test_the_payload_guard_refuses_a_payload_carrying_hidden_data(hermes_home: Path):
    """Directly exercises the last line of defence."""
    from learning_studio.components import build_component

    component = build_component(example("multiple_choice"), "component")
    unsafe = {**component.learner_payload(), "answer": {"option_id": "matrix"}}

    with pytest.raises(service.ValidationError, match="evaluator-only"):
        service._assert_payload_is_safe(unsafe, component)


def test_the_payload_guard_refuses_an_unknown_field(hermes_home: Path):
    from learning_studio.components import build_component

    component = build_component(example("multiple_choice"), "component")
    unsafe = {**component.learner_payload(), "surprise": 1}

    with pytest.raises(service.ValidationError, match="surprise"):
        service._assert_payload_is_safe(unsafe, component)


# ── Schema version guards ─────────────────────────────────────────────────


def test_a_newer_database_is_refused_rather_than_repaired(hermes_home: Path):
    prepare()

    with storage.connect() as conn:
        conn.execute("UPDATE schema_version SET version = ?", (storage.SCHEMA_VERSION + 1,))
        conn.commit()

    with pytest.raises(storage.IncompatibleSchemaError):
        prepare()


def test_the_refusal_names_no_path(hermes_home: Path):
    prepare()
    with storage.connect() as conn:
        conn.execute("UPDATE schema_version SET version = ?", (storage.SCHEMA_VERSION + 9,))
        conn.commit()

    with pytest.raises(storage.IncompatibleSchemaError) as refusal:
        prepare()

    assert str(hermes_home) not in str(refusal.value)


# ── The experience must be about the objective it names ───────────────────


@pytest.mark.parametrize("part", ["behavior", "condition", "standard"])
def test_an_objective_the_manifest_contradicts_is_refused(hermes_home: Path, part: str):
    """Ownership is not enough. A record that reads as evidence of progress
    against something it never tested is worse than no record."""
    track_id = confirmed_track()
    objective_id, objective = stored_objective(track_id)
    contradicted = {**objective, part: "something else entirely"}

    with pytest.raises(service.ValidationError, match=part):
        prepare(
            manifest=manifest(objective=contradicted),
            track_id=track_id,
            objective_id=objective_id,
        )


def test_objective_agreement_ignores_case_and_spacing(hermes_home: Path):
    track_id = confirmed_track()
    objective_id, objective = stored_objective(track_id)
    reworded = {key: f"  {value.upper()}  " for key, value in objective.items()}

    result = prepare(
        manifest=manifest(objective=reworded), track_id=track_id, objective_id=objective_id
    )

    assert result["objective_id"] == objective_id


def test_an_objective_from_another_track_is_refused(hermes_home: Path):
    first = confirmed_track(name="First")
    second = confirmed_track(name="Second")
    objective_id, objective = stored_objective(first)

    with pytest.raises(service.NotFoundError):
        prepare(
            manifest=manifest(objective=objective),
            track_id=second,
            objective_id=objective_id,
        )


def test_a_contradicted_objective_stores_nothing(hermes_home: Path):
    track_id = confirmed_track()
    objective_id, _ = stored_objective(track_id)

    with pytest.raises(service.ValidationError):
        prepare(manifest=manifest(), track_id=track_id, objective_id=objective_id)

    assert rows("SELECT * FROM experiences") == []


# ── Relationships the database itself enforces ────────────────────────────


def test_the_schema_refuses_an_objective_belonging_to_another_track(hermes_home: Path):
    """The composite key spans the track, so the row is unstorable."""
    first = confirmed_track(name="First")
    second = confirmed_track(name="Second")
    objective_id, objective = stored_objective(first)
    prepare(manifest=manifest(objective=objective), track_id=first, objective_id=objective_id)
    owner = rows("SELECT * FROM experiences")[0]

    with storage.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO experiences"
            " (id, learner_id, profile_id, track_id, objective_id, manifest_schema_version,"
            "  title, objective_behavior, objective_condition, objective_standard, instructions,"
            "  ui_locale, expected_duration_minutes, difficulty, accessibility,"
            "  source_references, delivery, component_count, created_at, updated_at)"
            " VALUES ('x', ?, ?, ?, ?, 1, 't', 'b', 'c', 's', 'i', 'en', 5, 'introductory',"
            " '{}', '[]', '{}', 1, 'now', 'now')",
            (owner["learner_id"], owner["profile_id"], second, objective_id),
        )


def test_the_schema_refuses_an_objective_named_without_a_track(hermes_home: Path):
    track_id = confirmed_track()
    objective_id, objective = stored_objective(track_id)
    prepare(manifest=manifest(objective=objective), track_id=track_id, objective_id=objective_id)
    owner = rows("SELECT * FROM experiences")[0]

    with storage.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO experiences"
            " (id, learner_id, profile_id, track_id, objective_id, manifest_schema_version,"
            "  title, objective_behavior, objective_condition, objective_standard, instructions,"
            "  ui_locale, expected_duration_minutes, difficulty, accessibility,"
            "  source_references, delivery, component_count, created_at, updated_at)"
            " VALUES ('x', ?, ?, NULL, ?, 1, 't', 'b', 'c', 's', 'i', 'en', 5, 'introductory',"
            " '{}', '[]', '{}', 1, 'now', 'now')",
            (owner["learner_id"], owner["profile_id"], objective_id),
        )


def test_deleting_an_objective_works_and_takes_its_experiences_with_it(hermes_home: Path):
    """The v3 schema raised ``NOT NULL constraint failed`` on any deletion.

    The action is now ``CASCADE``, which agrees with what a track deletion
    already does to the same rows.
    """
    track_id = confirmed_track()
    objective_id, objective = stored_objective(track_id)
    prepare(manifest=manifest(objective=objective), track_id=track_id, objective_id=objective_id)
    prepare()  # a second experience, attached to nothing

    with storage.connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM objectives")
        conn.commit()

    remaining = rows("SELECT objective_id FROM experiences")
    assert len(remaining) == 1
    assert remaining[0]["objective_id"] is None


def test_the_schema_refuses_an_evaluator_row_naming_another_experience(hermes_home: Path):
    """Evaluator data must belong to the experience its component belongs to."""
    prepare()
    component = rows("SELECT * FROM experience_components")[0]

    with storage.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM experience_component_evaluations")
        conn.execute(
            "INSERT INTO experience_component_evaluations"
            " (component_id, experience_id, learner_id, profile_id, evaluation, created_at)"
            " VALUES (?, 'a-different-experience', ?, ?, '{}', 'now')",
            (component["id"], component["learner_id"], component["profile_id"]),
        )


def test_the_schema_refuses_an_evaluator_row_for_another_learner(hermes_home: Path):
    prepare()
    component = rows("SELECT * FROM experience_components")[0]

    with storage.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM experience_component_evaluations")
        conn.execute(
            "INSERT INTO experience_component_evaluations"
            " (component_id, experience_id, learner_id, profile_id, evaluation, created_at)"
            " VALUES (?, ?, 'someone-else', ?, '{}', 'now')",
            (component["id"], component["experience_id"], component["profile_id"]),
        )


def test_deleting_an_experience_cascades_to_both_child_tables(hermes_home: Path):
    prepare()

    with storage.connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM experiences")
        conn.commit()

    assert rows("SELECT * FROM experience_components") == []
    assert rows("SELECT * FROM experience_component_evaluations") == []


# ── The alias record is versioned ─────────────────────────────────────────


def test_a_prepared_component_records_which_alias_scheme_it_used(hermes_home, principal):
    """The marker is what lets translation fail closed.

    Without it, "no mapping", "an incomplete mapping" and "prepared before
    aliasing existed" are indistinguishable, and the only reading that does not
    break the third is the identity — which is exactly the fallback that stored a
    learner-facing alias as an evaluator identifier.
    """
    from learning_studio import service
    from learning_studio.service import ALIAS_SCHEME
    from tests.component_examples import example, manifest

    result = service.prepare_experience(
        principal=principal,
        manifest=manifest([example("multiple_choice", id="q-one")]),
    )

    aliases = service.component_aliases(
        principal=principal,
        experience_id=result["experience_id"],
        component_key="q-one",
    )

    assert isinstance(aliases, dict)
    assert aliases, "an aliased component reported no mapping"
    assert ALIAS_SCHEME == 1

    stored = json.loads(_evaluation_row(result["experience_id"], "q-one")["evaluation"])
    assert stored["alias_scheme"] == ALIAS_SCHEME
    assert set(stored["aliases"]) == set(aliases)


def test_a_component_without_the_marker_reports_no_alias_record(hermes_home, principal):
    """A row written before aliasing reads as `None`, not as an empty mapping."""
    from learning_studio import service, storage
    from learning_studio.config import load_config
    from tests.component_examples import example, manifest

    result = service.prepare_experience(
        principal=principal,
        manifest=manifest([example("multiple_choice", id="q-one")]),
    )
    row = _evaluation_row(result["experience_id"], "q-one")
    legacy = json.loads(row["evaluation"])
    legacy.pop("alias_scheme")
    legacy.pop("aliases")
    with storage.connect(load_config()) as conn:
        conn.execute(
            "UPDATE experience_component_evaluations SET evaluation = ? WHERE component_id = ?",
            (json.dumps(legacy), row["component_id"]),
        )

    assert (
        service.component_aliases(
            principal=principal,
            experience_id=result["experience_id"],
            component_key="q-one",
        )
        is None
    )


def test_an_unknown_component_reports_no_alias_record(hermes_home, principal):
    from learning_studio import service
    from tests.component_examples import example, manifest

    result = service.prepare_experience(
        principal=principal,
        manifest=manifest([example("multiple_choice", id="q-one")]),
    )

    assert (
        service.component_aliases(
            principal=principal,
            experience_id=result["experience_id"],
            component_key="no-such-card",
        )
        is None
    )


def test_another_learner_cannot_read_an_alias_mapping(hermes_home, principal, other_principal):
    """The mapping is scoped in SQL like every other learner-owned read."""
    from learning_studio import service
    from tests.component_examples import example, manifest

    result = service.prepare_experience(
        principal=principal,
        manifest=manifest([example("multiple_choice", id="q-one")]),
    )

    assert (
        service.component_aliases(
            principal=other_principal,
            experience_id=result["experience_id"],
            component_key="q-one",
        )
        is None
    )


def _evaluation_row(experience_id: str, component_key: str):
    from learning_studio import storage
    from learning_studio.config import load_config

    with storage.connect(load_config()) as conn:
        return conn.execute(
            "SELECT e.component_id, e.evaluation"
            "  FROM experience_components AS c"
            "  JOIN experience_component_evaluations AS e ON e.component_id = c.id"
            " WHERE c.experience_id = ? AND c.component_key = ?",
            (experience_id, component_key),
        ).fetchone()
