"""Upgrading a database that an earlier commit of this branch created.

An applied migration is immutable. Once a database records version 1, that
number is a claim about what it contains, and editing migration 1 afterwards
cannot change what is already on disk — it only makes the claim false. The
defect these tests exist for was exactly that: ``consented_need`` was added
to migration 1 without advancing the version, so a database written by
``08e0719`` reported itself current while missing the column, and the next
accessibility candidate failed with ``no column named consented_need``.

The v1 database here is built by running the *real* migration 1 and nothing
else, so it is byte-identical to what that commit produced.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from learning_studio import service, storage
from learning_studio.identity import Principal

LEARNER = Principal(
    profile="default", platform="telegram", user_id="8800", source="gateway_session"
)

CONSENT = {
    "consent_statement": "Please remember that I need captions",
    "needs": ["captions on audio"],
}


def _valid_candidate(**overrides):
    payload = {
        "category": "durable_preference",
        "statement": "Prefers worked examples first",
        "evidence_summary": "The learner asked for this to be remembered",
        "origin": "explicit_durable_preference",
        "confirmation_state": "learner_confirmed",
    }
    payload.update(overrides)
    return payload


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _build_database_at(version: int) -> None:
    """Create a database at an older schema version, via the real migrations.

    Restores ``MIGRATIONS`` by hand rather than through monkeypatch, whose
    ``undo()`` would also revert the ``HERMES_HOME`` the storage fixture set.
    """
    saved = storage.MIGRATIONS
    storage.MIGRATIONS = [m for m in saved if m.version <= version]
    try:
        storage.initialize()
    finally:
        storage.MIGRATIONS = saved


def _build_v1_database() -> None:
    """Create a database at schema version 1, as ``08e0719`` left it."""
    _build_database_at(1)


def _build_v2_database() -> None:
    """Create a database at schema version 2 — the state PR 03 shipped."""
    _build_database_at(2)


def _seed_v1_rows(conn: sqlite3.Connection) -> None:
    """Insert the rows a v1 profile would plausibly hold.

    The accessibility candidate is one v1 would have accepted under the
    defective rule: consent to remember captions, attached to a diagnosis.
    """
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO learners"
        " (id, profile_id, principal_digest, platform, created_at, updated_at)"
        " VALUES ('L1', 'default', 'digest-1', 'telegram', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO tracks"
        " (id, learner_id, profile_id, name, status, confirmed_at, created_at, updated_at)"
        " VALUES ('T1', 'L1', 'default', 'Existing track', 'active', ?, ?, ?)",
        (now, now, now),
    )
    conn.execute(
        "INSERT INTO learning_contexts"
        " (id, learner_id, profile_id, scope, track_id, expires_at, created_at, updated_at)"
        " VALUES ('C1', 'L1', 'default', 'track', 'T1', NULL, ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO context_values"
        " (id, context_id, learner_id, profile_id, field, value, provenance,"
        "  confirmed, created_at, updated_at)"
        " VALUES ('V1', 'C1', 'L1', 'default', 'goal', 'existing goal',"
        " 'confirmed_track', 1, ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO objectives"
        " (id, track_id, learner_id, profile_id, behavior, condition, standard,"
        "  status, created_at, updated_at)"
        " VALUES ('O1', 'T1', 'L1', 'default', 'b', 'c', 's', 'active', ?, ?)",
        (now, now),
    )
    common = (
        "INSERT INTO memory_candidates"
        " (id, learner_id, profile_id, track_id, category, statement, evidence_summary,"
        "  origin, evidence_count, confidence, durability, confirmation_state,"
        "  recommended_action, replaces, consent_reference, created_at, updated_at)"
        " VALUES (?, 'L1', 'default', NULL, ?, ?, ?, ?, NULL, 'medium', 'durable',"
        " ?, 'add', NULL, ?, ?, ?)"
    )
    conn.execute(
        common,
        (
            "MC-ordinary",
            "durable_preference",
            "Prefers worked examples first",
            "Said so directly",
            "explicit_durable_preference",
            "unconfirmed",
            None,
            now,
            now,
        ),
    )
    conn.execute(
        common,
        (
            "MC-legacy-accessibility",
            "accessibility",
            "The learner has ADHD",
            "The learner stated this",
            "explicit_durable_preference",
            "learner_confirmed",
            "Remember that I need captions",
            now,
            now,
        ),
    )
    conn.commit()


# ── Migration 1 is immutable ──────────────────────────────────────────────


def test_migration_one_does_not_define_the_new_column():
    """Editing an applied migration cannot upgrade a database that ran it."""
    first = next(m for m in storage.MIGRATIONS if m.version == 1)

    assert "consented_need" not in " ".join(first.statements), (
        "migration 1 has been edited after release; a v1 database on disk will "
        "never gain the column, so this must be a new migration instead"
    )


def test_migrations_are_contiguous_and_the_version_is_the_last_one():
    versions = [m.version for m in storage.MIGRATIONS]

    assert versions == list(range(1, len(versions) + 1))
    assert storage.SCHEMA_VERSION == versions[-1] == 9


def test_a_v8_database_upgrades_with_a_separate_alias_binding_store(hermes_home: Path):
    _build_database_at(8)

    storage.initialize()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == 9
        columns = _columns(conn, "experience_component_alias_bindings")
        assert {
            "component_id",
            "experience_id",
            "learner_id",
            "profile_id",
            "binding_scheme",
            "binding_digest",
            "created_at",
        } == columns


def test_a_v8_alias_record_upgrades_unbound_and_new_records_are_bound(
    hermes_home: Path,
):
    """Migration cannot manufacture exact correspondence for existing mappings."""
    from learning_studio.service import AliasState
    from tests.component_examples import example, manifest

    saved = storage.MIGRATIONS
    storage.MIGRATIONS = [migration for migration in saved if migration.version <= 8]
    try:
        old = service.prepare_experience(
            principal=LEARNER,
            manifest=manifest([example("multiple_choice", id="old-question")]),
        )
    finally:
        storage.MIGRATIONS = saved

    with storage.connect() as conn:
        old_evaluator = conn.execute(
            "SELECT evaluation FROM experience_component_evaluations WHERE experience_id = ?",
            (old["experience_id"],),
        ).fetchone()["evaluation"]
        assert '"alias_scheme":2' in old_evaluator
        assert storage.read_schema_version(conn) == 8

    storage.initialize()

    old_aliases = service.component_aliases(
        principal=LEARNER,
        experience_id=old["experience_id"],
        component_key="old-question",
    )
    assert old_aliases.state is AliasState.UNRESOLVED
    assert not old_aliases.translates

    new = service.prepare_experience(
        principal=LEARNER,
        manifest=manifest([example("multiple_choice", id="new-question")]),
    )
    with storage.connect() as conn:
        rows = conn.execute(
            "SELECT c.experience_id, b.binding_scheme, b.binding_digest"
            "  FROM experience_component_alias_bindings AS b"
            "  JOIN experience_components AS c ON c.id = b.component_id"
        ).fetchall()

    assert {
        (
            row["experience_id"],
            row["binding_scheme"],
            len(row["binding_digest"]) if row["binding_digest"] is not None else None,
        )
        for row in rows
    } == {
        (old["experience_id"], 2, None),
        (new["experience_id"], 3, 64),
    }


def test_a_failing_migration_nine_rolls_back_the_binding_store(hermes_home: Path, monkeypatch):
    _build_database_at(8)
    real = list(storage.MIGRATIONS)
    broken_nine = storage.Migration(
        version=9,
        statements=(*real[8].statements, "THIS IS NOT SQL"),
    )
    monkeypatch.setattr(storage, "MIGRATIONS", [*real[:8], broken_nine])

    with pytest.raises(storage.MigrationError):
        storage.initialize()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == 8
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type = 'table' AND name = 'experience_component_alias_bindings'"
            ).fetchone()
            is None
        )


def test_a_v7_database_upgrades_to_managed_assets_without_rewriting_prior_state(
    hermes_home: Path,
):
    _build_database_at(7)
    now = "2026-01-01T00:00:00+00:00"
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO learners"
            " (id, profile_id, principal_digest, platform, created_at, updated_at)"
            " VALUES ('L-before-assets', 'default', 'digest-before-assets', 'telegram', ?, ?)",
            (now, now),
        )

    storage.initialize()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == 9
        assert (
            conn.execute("SELECT id FROM learners WHERE id = 'L-before-assets'").fetchone()["id"]
            == "L-before-assets"
        )
        columns = _columns(conn, "managed_assets")
        assert {
            "id",
            "learner_id",
            "profile_id",
            "track_id",
            "sha256",
            "mime_type",
            "storage_name",
            "generation_prompt",
        } <= columns
        assert "source_path" not in columns


def test_a_failing_migration_eight_rolls_back_the_asset_schema(hermes_home: Path, monkeypatch):
    _build_database_at(7)
    real = list(storage.MIGRATIONS)
    broken_eight = storage.Migration(
        version=8,
        statements=(*real[7].statements, "THIS IS NOT SQL"),
    )
    monkeypatch.setattr(storage, "MIGRATIONS", [*real[:7], broken_eight])

    with pytest.raises(storage.MigrationError):
        storage.initialize()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == 7
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'managed_assets'"
            ).fetchone()
            is None
        )


def test_migration_two_adds_the_column():
    second = next(m for m in storage.MIGRATIONS if m.version == 2)
    body = " ".join(second.statements)

    assert "consented_need" in body
    assert "ALTER TABLE" in body.upper()


# ── The exact v1 → v2 upgrade ─────────────────────────────────────────────


def test_a_v1_database_reports_version_1_without_the_column(hermes_home: Path):
    """The starting state, verified rather than assumed."""
    _build_v1_database()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == 1
        assert "consented_need" not in _columns(conn, "memory_candidates")


def test_initialize_upgrades_a_v1_database_to_the_current_version(hermes_home: Path):
    """Migrations are cumulative: a v1 database runs 2 and 3 in one go."""
    _build_v1_database()

    storage.initialize()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == storage.SCHEMA_VERSION
        assert "consented_need" in _columns(conn, "memory_candidates")


def test_the_upgrade_is_idempotent(hermes_home: Path):
    _build_v1_database()

    storage.initialize()
    storage.initialize()
    storage.initialize()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == storage.SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"] == 1


# ── Data carried across the upgrade ───────────────────────────────────────


def test_the_upgrade_preserves_unrelated_data(hermes_home: Path):
    _build_v1_database()
    with storage.connect() as conn:
        _seed_v1_rows(conn)

    storage.initialize()

    with storage.connect() as conn:
        for table in ("learners", "tracks", "learning_contexts", "context_values", "objectives"):
            count = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            assert count == 1, f"{table} lost rows during the upgrade"
        value = conn.execute("SELECT value FROM context_values").fetchone()["value"]
        assert value == "existing goal"


def test_a_non_sensitive_candidate_survives_with_a_null_binding(hermes_home: Path):
    _build_v1_database()
    with storage.connect() as conn:
        _seed_v1_rows(conn)

    storage.initialize()

    with storage.connect() as conn:
        row = conn.execute("SELECT * FROM memory_candidates WHERE id = 'MC-ordinary'").fetchone()

    assert row is not None, "an ordinary candidate was removed by the upgrade"
    assert row["consented_need"] is None
    assert row["statement"] == "Prefers worked examples first"


def test_a_legacy_accessibility_candidate_does_not_survive(hermes_home: Path):
    """Its consent scope cannot be proved, so it cannot be trusted.

    v1 accepted "remember I need captions" as authorisation for an ADHD
    diagnosis. There is no honest way to derive a consented need for that
    row: parsing the consent prose would be inventing consent, and keeping it
    would carry the old defect forward into a schema that promises otherwise.
    """
    _build_v1_database()
    with storage.connect() as conn:
        _seed_v1_rows(conn)

    storage.initialize()

    with storage.connect() as conn:
        row = conn.execute(
            "SELECT * FROM memory_candidates WHERE id = 'MC-legacy-accessibility'"
        ).fetchone()
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM memory_candidates WHERE category = 'accessibility'"
        ).fetchone()["n"]
        blob = " ".join(str(r[0]) for r in conn.execute("SELECT statement FROM memory_candidates"))

    assert row is None
    assert remaining == 0
    assert "ADHD" not in blob


def test_the_legacy_candidate_is_absent_from_a_later_read(hermes_home: Path):
    _build_v1_database()
    with storage.connect() as conn:
        _seed_v1_rows(conn)

    storage.initialize()

    with storage.connect() as conn:
        rows = service._candidates_json(conn, "default", "L1")

    assert [r["category"] for r in rows] == ["durable_preference"]


# ── Behaviour after the upgrade ───────────────────────────────────────────


def test_a_candidate_saves_after_the_upgrade(hermes_home: Path):
    """The failure the reviewer reported: this raised OperationalError.

    The candidate is an ordinary preference now. An accessibility one is
    refused whatever the schema version, because nothing in a tool call can
    establish that a learner consented to it.
    """
    _build_v1_database()
    with storage.connect() as conn:
        _seed_v1_rows(conn)

    result = service.save_context(principal=LEARNER, memory_candidates=[_valid_candidate()])

    assert result["outcome"]["memory_candidates"]["accepted"], result["outcome"]


def test_the_candidate_round_trips_after_the_upgrade(hermes_home: Path):
    _build_v1_database()
    service.save_context(principal=LEARNER, memory_candidates=[_valid_candidate()])

    stored = service.get_context(principal=LEARNER, include_memory_candidates=True)
    candidate = stored["memory_candidates"][0]

    assert candidate["statement"] == "Prefers worked examples first"
    # Claimed as an explicit durable preference and confirmed by the learner;
    # recorded as what can actually be shown, which is that a model proposed
    # it. Both halves are downgraded, not just the confirmation flag.
    assert candidate["origin"] == "model_proposed"
    assert candidate["confirmation_state"] == "unconfirmed"
    assert candidate["created_at"] and candidate["updated_at"]


def test_a_sensitive_candidate_is_still_rejected_after_the_upgrade(hermes_home: Path):
    _build_v1_database()

    result = service.save_context(
        principal=LEARNER,
        accessibility_consent=CONSENT,
        memory_candidates=[
            _valid_candidate(category="accessibility", statement="ADHD", consented_need="ADHD")
        ],
    )

    assert result["outcome"]["memory_candidates"]["accepted"] == []
    with storage.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM memory_candidates WHERE category = 'accessibility'"
        ).fetchone()["n"]
    assert count == 0


# ── A fresh database ──────────────────────────────────────────────────────


def test_a_fresh_database_applies_every_migration(hermes_home: Path):
    storage.initialize()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == storage.SCHEMA_VERSION
        assert "consented_need" in _columns(conn, "memory_candidates")

        objects = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master")}
        for table in (
            "schema_version",
            "studio_meta",
            "learners",
            "tracks",
            "learning_contexts",
            "context_values",
            "context_revisions",
            "objectives",
            "memory_candidates",
            "experiences",
            "experience_components",
            "experience_component_evaluations",
        ):
            assert table in objects, f"{table} missing from a fresh database"
        for index in (
            "idx_tracks_owner",
            "idx_one_temporary_context_per_learner",
            "idx_one_context_per_track",
            "idx_contexts_expiry",
            "idx_context_values_owner",
            "idx_revisions_owner",
            "idx_objectives_owner",
            "idx_candidates_owner",
            "idx_objectives_identity",
            "idx_experiences_owner",
            "idx_experience_components_owner",
            "idx_experience_evaluations_owner",
        ):
            assert index in objects, f"{index} missing from a fresh database"

        # Composite ownership constraints survived the split.
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tracks"
                " (id, learner_id, profile_id, name, status, confirmed_at,"
                "  created_at, updated_at)"
                " VALUES ('x', 'nobody', 'default', 'n', 'active', 'now', 'now', 'now')"
            )


def test_a_fresh_database_accepts_and_rejects_correctly(hermes_home: Path):
    accepted = service.save_context(principal=LEARNER, memory_candidates=[_valid_candidate()])
    assert len(accepted["outcome"]["memory_candidates"]["accepted"]) == 1

    rejected = service.save_context(
        principal=LEARNER,
        accessibility_consent=CONSENT,
        memory_candidates=[
            _valid_candidate(category="accessibility", statement="ADHD", consented_need="ADHD")
        ],
    )
    assert rejected["outcome"]["memory_candidates"]["accepted"] == []


# ── Rollback ──────────────────────────────────────────────────────────────


def test_a_failing_migration_two_rolls_back_completely(hermes_home: Path, monkeypatch):
    """Drives the production path with a real Migration whose SQL fails."""
    _build_v1_database()
    with storage.connect() as conn:
        _seed_v1_rows(conn)

    first = storage.MIGRATIONS[0]
    monkeypatch.setattr(
        storage,
        "MIGRATIONS",
        [
            first,
            storage.Migration(
                version=2,
                statements=(
                    "DELETE FROM memory_candidates WHERE category = 'accessibility'",
                    "ALTER TABLE memory_candidates ADD COLUMN consented_need TEXT",
                    "CREATE TABLE canary (id TEXT PRIMARY KEY)",
                    "THIS IS NOT VALID SQL",
                ),
            ),
        ],
    )

    with pytest.raises(storage.MigrationError):
        storage.initialize()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == 1, "the version advanced despite failure"
        assert "consented_need" not in _columns(conn, "memory_candidates")
        objects = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert "canary" not in objects
        # The deletion is part of the same transaction, so it rolled back too.
        ids = {row["id"] for row in conn.execute("SELECT id FROM memory_candidates")}
        assert ids == {"MC-ordinary", "MC-legacy-accessibility"}
        # And the connection is still usable.
        assert conn.execute("SELECT 1").fetchone()[0] == 1


# ── Concurrency ───────────────────────────────────────────────────────────


def test_two_callers_upgrading_the_same_v1_database_both_succeed(hermes_home: Path):
    """Neither may see a duplicate-column or locked-database error."""
    _build_v1_database()

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def upgrade() -> None:
        try:
            barrier.wait(timeout=5)
            storage.initialize()
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=upgrade) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == [], f"concurrent upgrade failed: {errors}"
    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == storage.SCHEMA_VERSION
        assert "consented_need" in _columns(conn, "memory_candidates")


# ── The exact v2 → v3 upgrade ─────────────────────────────────────────────


def test_a_v2_database_reports_version_2_without_the_experience_tables(hermes_home: Path):
    """The starting state PR 03 shipped, verified rather than assumed."""
    _build_v2_database()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == 2
        objects = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert "experiences" not in objects


def test_initialize_upgrades_a_v2_database_to_the_current_version(hermes_home: Path):
    _build_v2_database()

    storage.initialize()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == storage.SCHEMA_VERSION
        objects = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert {"experiences", "experience_components", "experience_component_evaluations"} <= (
            objects
        )


def test_the_v2_upgrade_preserves_existing_learning_data(hermes_home: Path):
    _build_v1_database()
    with storage.connect() as conn:
        _seed_v1_rows(conn)
    _build_v2_database()

    storage.initialize()

    with storage.connect() as conn:
        for table in ("learners", "tracks", "learning_contexts", "context_values", "objectives"):
            count = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            assert count == 1, f"{table} lost rows during the v2 to v3 upgrade"


def test_a_v2_database_can_store_an_experience_after_upgrading(hermes_home: Path):
    """The upgrade is judged by whether the new feature actually works."""
    from tests.component_examples import manifest

    _build_v2_database()

    result = service.prepare_experience(principal=LEARNER, manifest=manifest())

    assert result["ok"] is True
    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM experiences").fetchone()["n"] == 1


def test_migration_three_is_additive_only(hermes_home: Path):
    """Every statement creates something; none rewrites what is already there.

    Asserted on each statement's leading keyword rather than by searching the
    text: ``ON DELETE CASCADE`` is a foreign-key clause, not a deletion, and a
    substring search would flag it while missing a ``DROP`` written on the
    second line of a statement.
    """
    third = next(m for m in storage.MIGRATIONS if m.version == 3)

    verbs = {statement.strip().split()[0].upper() for statement in third.statements}
    assert verbs == {"CREATE"}, f"migration 3 does more than create: {sorted(verbs)}"


def test_earlier_migrations_are_unchanged_by_the_new_one(hermes_home: Path):
    """An applied migration is immutable; version 3 must not have edited them."""
    first = next(m for m in storage.MIGRATIONS if m.version == 1)
    second = next(m for m in storage.MIGRATIONS if m.version == 2)

    assert "experiences" not in " ".join(first.statements)
    assert "experiences" not in " ".join(second.statements)


def test_a_failing_migration_three_rolls_back_completely(hermes_home: Path, monkeypatch):
    """Drives the production path with a real Migration whose SQL fails."""
    _build_v2_database()
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO learners"
            " (id, profile_id, principal_digest, platform, created_at, updated_at)"
            " VALUES ('L9', 'default', 'digest-9', 'telegram', 'now', 'now')"
        )
        conn.commit()

    real = list(storage.MIGRATIONS)
    monkeypatch.setattr(
        storage,
        "MIGRATIONS",
        [
            *real[:2],
            storage.Migration(
                version=3,
                statements=(*real[2].statements, "THIS IS NOT VALID SQL"),
            ),
        ],
    )

    with pytest.raises(storage.MigrationError):
        storage.initialize()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == 2, "the version advanced despite failure"
        objects = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert "experiences" not in objects, "a table survived a rolled-back migration"
        assert conn.execute("SELECT COUNT(*) AS n FROM learners").fetchone()["n"] == 1
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_two_callers_upgrading_a_v2_database_both_succeed(hermes_home: Path):
    _build_v2_database()

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def upgrade() -> None:
        try:
            barrier.wait(timeout=5)
            storage.initialize()
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=upgrade) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == [], f"concurrent upgrade failed: {errors}"
    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == storage.SCHEMA_VERSION


# ── The exact v3 → v4 upgrade ─────────────────────────────────────────────


def _build_v3_database() -> None:
    """Create a database at schema version 3 — the state this PR first had."""
    _build_database_at(3)


def _store_experience_under_v3():
    """Prepare a real experience while the database is still at version 3."""
    from tests.component_examples import example, manifest

    saved = storage.MIGRATIONS
    storage.MIGRATIONS = [m for m in saved if m.version <= 3]
    try:
        storage.initialize()
        return service.prepare_experience(
            principal=LEARNER,
            manifest=manifest(
                [example("multiple_choice", id="one"), example("short_answer", id="two")]
            ),
        )
    finally:
        storage.MIGRATIONS = saved


def test_a_v3_database_reports_version_3(hermes_home: Path):
    _build_v3_database()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == 3


def test_initialize_upgrades_a_v3_database_to_the_current_version(hermes_home: Path):
    _build_v3_database()

    storage.initialize()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == storage.SCHEMA_VERSION


def test_the_v3_upgrade_preserves_experiences_and_their_order(hermes_home: Path):
    prepared = _store_experience_under_v3()

    storage.initialize()

    with storage.connect() as conn:
        experience = conn.execute("SELECT * FROM experiences").fetchone()
        components = conn.execute(
            "SELECT position, component_key FROM experience_components ORDER BY position"
        ).fetchall()
        evaluations = conn.execute(
            "SELECT COUNT(*) AS n FROM experience_component_evaluations"
        ).fetchone()["n"]

    assert experience["id"] == prepared["experience_id"]
    assert [(row["position"], row["component_key"]) for row in components] == [
        (1, "one"),
        (2, "two"),
    ]
    assert evaluations == 2


def test_the_v3_upgrade_keeps_the_learner_and_evaluator_halves_apart(hermes_home: Path):
    from tests.component_examples import CANARY

    _store_experience_under_v3()

    storage.initialize()

    with storage.connect() as conn:
        payloads = " ".join(
            str(row["learner_payload"])
            for row in conn.execute("SELECT learner_payload FROM experience_components")
        )
        evaluations = " ".join(
            str(row["evaluation"])
            for row in conn.execute("SELECT evaluation FROM experience_component_evaluations")
        )

    assert CANARY not in payloads
    assert CANARY in evaluations


def test_the_v3_upgrade_repairs_an_objective_from_another_track(hermes_home: Path):
    """A row v3 allowed and v4 forbids keeps the experience, loses the link."""
    _store_experience_under_v3()

    with storage.connect() as conn:
        experience = conn.execute("SELECT * FROM experiences").fetchone()
        now = "2026-01-01T00:00:00+00:00"
        conn.execute(
            "INSERT INTO tracks (id, learner_id, profile_id, name, status, confirmed_at,"
            " created_at, updated_at) VALUES ('TA', ?, ?, 'A', 'active', ?, ?, ?)",
            (experience["learner_id"], experience["profile_id"], now, now, now),
        )
        conn.execute(
            "INSERT INTO tracks (id, learner_id, profile_id, name, status, confirmed_at,"
            " created_at, updated_at) VALUES ('TB', ?, ?, 'B', 'active', ?, ?, ?)",
            (experience["learner_id"], experience["profile_id"], now, now, now),
        )
        conn.execute(
            "INSERT INTO objectives (id, track_id, learner_id, profile_id, behavior, condition,"
            " standard, status, created_at, updated_at)"
            " VALUES ('OA', 'TA', ?, ?, 'b', 'c', 's', 'active', ?, ?)",
            (experience["learner_id"], experience["profile_id"], now, now),
        )
        # The mismatch v3 could not prevent: track B, objective from track A.
        conn.execute(
            "UPDATE experiences SET track_id = 'TB', objective_id = 'OA' WHERE id = ?",
            (experience["id"],),
        )
        conn.commit()

    storage.initialize()

    with storage.connect() as conn:
        repaired = conn.execute("SELECT * FROM experiences").fetchone()

    assert repaired is not None, "the experience was destroyed rather than repaired"
    assert repaired["track_id"] == "TB"
    assert repaired["objective_id"] is None


def test_the_v3_upgrade_drops_an_unattributable_evaluator_row(hermes_home: Path):
    _store_experience_under_v3()

    with storage.connect() as conn:
        # v3 constrained only ``component_id``, so an evaluator row could name
        # any experience at all. Rewrite one to name a nonexistent one.
        stray = conn.execute(
            "SELECT * FROM experience_components ORDER BY position DESC LIMIT 1"
        ).fetchone()
        conn.execute(
            "UPDATE experience_component_evaluations SET experience_id = 'no-such-experience'"
            " WHERE component_id = ?",
            (stray["id"],),
        )
        conn.commit()

    storage.initialize()

    with storage.connect() as conn:
        ids = {
            row["component_id"]
            for row in conn.execute("SELECT component_id FROM experience_component_evaluations")
        }

    assert stray["id"] not in ids, "an unattributable evaluator row survived the upgrade"
    assert len(ids) == 1, "the valid evaluator row was lost too"


def test_the_v3_upgrade_is_idempotent(hermes_home: Path):
    _store_experience_under_v3()

    storage.initialize()
    storage.initialize()
    storage.initialize()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == storage.SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) AS n FROM experiences").fetchone()["n"] == 1


def test_migration_four_uses_no_executescript_and_only_ordinary_statements(hermes_home: Path):
    """Statements, so the rebuild runs inside the caller's transaction."""
    fourth = next(m for m in storage.MIGRATIONS if m.version == 4)

    assert all(isinstance(statement, str) for statement in fourth.statements)
    assert "executescript" not in " ".join(fourth.statements).lower()


def test_earlier_migrations_were_not_edited_by_the_rebuild(hermes_home: Path):
    """Migrations 1 to 3 are applied history and must stay byte-for-byte."""
    for version in (1, 2, 3):
        body = " ".join(next(m for m in storage.MIGRATIONS if m.version == version).statements)
        assert "_v4" not in body, f"migration {version} was edited by the v4 rebuild"


def test_a_failing_migration_four_rolls_back_completely(hermes_home: Path, monkeypatch):
    prepared = _store_experience_under_v3()

    real = list(storage.MIGRATIONS)
    monkeypatch.setattr(
        storage,
        "MIGRATIONS",
        [
            *real[:3],
            storage.Migration(version=4, statements=(*real[3].statements, "THIS IS NOT SQL")),
        ],
    )

    with pytest.raises(storage.MigrationError):
        storage.initialize()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == 3, "the version advanced despite failure"
        experience = conn.execute("SELECT * FROM experiences").fetchone()
        components = conn.execute("SELECT COUNT(*) AS n FROM experience_components").fetchone()
        objects = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master")}

    assert experience["id"] == prepared["experience_id"], "the experience was lost"
    assert components["n"] == 2
    assert not any(name.endswith("_v4") for name in objects), "a scratch table survived"


def test_two_callers_upgrading_a_v3_database_both_succeed(hermes_home: Path):
    _store_experience_under_v3()

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def upgrade() -> None:
        try:
            barrier.wait(timeout=5)
            storage.initialize()
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=upgrade) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == [], f"concurrent upgrade failed: {errors}"
    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == storage.SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) AS n FROM experiences").fetchone()["n"] == 1


def test_an_experience_still_prepares_after_the_v3_upgrade(hermes_home: Path):
    from tests.component_examples import manifest

    _store_experience_under_v3()
    storage.initialize()

    result = service.prepare_experience(principal=LEARNER, manifest=manifest())

    assert result["ok"] is True


# ── A newer database is refused before it is touched ──────────────────────


def test_an_unsupported_newer_database_is_left_byte_for_byte_unchanged(hermes_home: Path):
    """The reported reproduction: refusal used to happen *after* WAL was set.

    ``connect`` applies the configured journal mode, which is persistent, so a
    database from a future version was converted and only then rejected. The
    compatibility question is now asked through a read-only handle first.
    """
    storage.initialize()
    database = storage.database_path()

    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("UPDATE schema_version SET version = 99")
        conn.commit()

    def journal_mode() -> str:
        probe = sqlite3.connect(database)
        try:
            return str(probe.execute("PRAGMA journal_mode").fetchone()[0])
        finally:
            probe.close()

    before_mode = journal_mode()
    before_files = sorted(path.name for path in database.parent.iterdir())
    before_bytes = database.read_bytes()

    with pytest.raises(storage.IncompatibleSchemaError):
        storage.initialize()

    assert journal_mode() == before_mode == "delete", "the journal mode was changed"
    assert sorted(path.name for path in database.parent.iterdir()) == before_files
    assert database.read_bytes() == before_bytes, "the refused database was modified"


def test_a_refused_database_gains_no_wal_or_shm_files(hermes_home: Path):
    storage.initialize()
    database = storage.database_path()
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("UPDATE schema_version SET version = 99")
        conn.commit()

    with pytest.raises(storage.IncompatibleSchemaError):
        storage.initialize()

    stray = sorted(p.name for p in database.parent.iterdir() if p.name != database.name)
    assert stray == []


def test_a_supported_database_is_still_configured_normally(hermes_home: Path):
    """The guard must not stop an ordinary database from being set up."""
    storage.initialize()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == storage.SCHEMA_VERSION
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


# ── The exact v4 → v5 upgrade ─────────────────────────────────────────────


def _build_v4_database() -> None:
    """Create a database at schema version 4 — the state the last commit left."""
    _build_database_at(4)


def _candidate_under_v4() -> str:
    """Seed a candidate row the way schema 4 stored one.

    Written with SQL rather than through ``save_context``: the service is the
    *current* code, and inserting through it would write an ``expires_at`` a
    v4 database has no column for. What a v4 profile actually holds is a row
    without that column, which is what this creates.
    """
    _build_v4_database()
    now = "2026-01-01T00:00:00+00:00"
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO learners"
            " (id, profile_id, principal_digest, platform, created_at, updated_at)"
            " VALUES ('L4', 'default', 'digest-4', 'telegram', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO memory_candidates"
            " (id, learner_id, profile_id, track_id, category, statement, evidence_summary,"
            "  origin, evidence_count, confidence, durability, confirmation_state,"
            "  recommended_action, replaces, consent_reference, consented_need,"
            "  created_at, updated_at)"
            " VALUES ('MC-v4', 'L4', 'default', NULL, 'durable_preference',"
            " 'Carried across the upgrade', 'Said so directly', 'repeated_evidence', 5,"
            " 'medium', 'durable', 'unconfirmed', 'add', NULL, NULL, NULL, ?, ?)",
            (now, now),
        )
        conn.commit()
    return "MC-v4"


def test_a_v4_database_reports_version_4_without_the_expiry_column(hermes_home: Path):
    _build_v4_database()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == 4
        assert "expires_at" not in _columns(conn, "memory_candidates")


def test_initialize_upgrades_a_v4_database_to_the_current_version(hermes_home: Path):
    _build_v4_database()

    storage.initialize()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == storage.SCHEMA_VERSION
        assert "expires_at" in _columns(conn, "memory_candidates")


def test_the_v4_upgrade_preserves_candidates(hermes_home: Path):
    candidate_id = _candidate_under_v4()

    storage.initialize()

    with storage.connect() as conn:
        row = conn.execute(
            "SELECT * FROM memory_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()

    assert row is not None, "the candidate was lost in the upgrade"
    assert row["statement"] == "Carried across the upgrade"
    # Pre-existing rows are durable: an expiry cannot be invented for them.
    assert row["expires_at"] is None


def test_the_v4_upgrade_drops_legacy_non_durable_candidates(hermes_home: Path):
    """v4 persisted both labels forever; v5 must not make that mistake permanent."""
    _build_v4_database()
    now = "2026-01-01T00:00:00+00:00"
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO learners"
            " (id, profile_id, principal_digest, platform, created_at, updated_at)"
            " VALUES ('L4', 'default', 'digest-4', 'telegram', ?, ?)",
            (now, now),
        )
        for candidate_id, durability in (("session-old", "session"), ("short-old", "short_term")):
            conn.execute(
                "INSERT INTO memory_candidates"
                " (id, learner_id, profile_id, track_id, category, statement, evidence_summary,"
                "  origin, evidence_count, confidence, durability, confirmation_state,"
                "  recommended_action, replaces, consent_reference, consented_need,"
                "  created_at, updated_at)"
                " VALUES (?, 'L4', 'default', NULL, 'durable_preference', ?, 'Legacy row',"
                " 'repeated_evidence', 5, 'medium', ?, 'unconfirmed', 'add', NULL, NULL,"
                " NULL, ?, ?)",
                (candidate_id, candidate_id, durability, now, now),
            )
        conn.commit()

    storage.initialize()

    with storage.connect() as conn:
        rows = conn.execute("SELECT id FROM memory_candidates").fetchall()
    assert rows == []


def test_the_v4_upgrade_downgrades_legacy_authority_claims(hermes_home: Path):
    _build_v4_database()
    now = "2026-01-01T00:00:00+00:00"
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO learners"
            " (id, profile_id, principal_digest, platform, created_at, updated_at)"
            " VALUES ('L4', 'default', 'digest-4', 'telegram', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO memory_candidates"
            " (id, learner_id, profile_id, track_id, category, statement, evidence_summary,"
            "  origin, evidence_count, confidence, durability, confirmation_state,"
            "  recommended_action, replaces, consent_reference, consented_need,"
            "  created_at, updated_at)"
            " VALUES ('authority-old', 'L4', 'default', NULL, 'long_term_goal',"
            " 'Become a surgeon', 'Allegedly confirmed', 'confirmed_long_term_goal', NULL,"
            " 'medium', 'durable', 'learner_confirmed', 'add', NULL, NULL, NULL, ?, ?)",
            (now, now),
        )
        conn.commit()

    storage.initialize()

    with storage.connect() as conn:
        row = conn.execute(
            "SELECT origin, confirmation_state FROM memory_candidates WHERE id = 'authority-old'"
        ).fetchone()
    assert dict(row) == {"origin": "model_proposed", "confirmation_state": "unconfirmed"}


def test_the_v4_upgrade_is_idempotent(hermes_home: Path):
    _candidate_under_v4()

    storage.initialize()
    storage.initialize()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == storage.SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) AS n FROM memory_candidates").fetchone()["n"] == 1


def test_migration_five_uses_only_ordinary_statements(hermes_home: Path):
    fifth = next(m for m in storage.MIGRATIONS if m.version == 5)

    assert all(isinstance(statement, str) for statement in fifth.statements)
    assert "executescript" not in " ".join(fifth.statements).lower()


def test_migrations_one_to_four_were_not_edited_by_the_rebuild(hermes_home: Path):
    """Applied history stays byte-for-byte, however many rebuilds follow."""
    for version in (1, 2, 3, 4):
        body = " ".join(next(m for m in storage.MIGRATIONS if m.version == version).statements)
        assert "_v5" not in body, f"migration {version} was edited by the v5 rebuild"


def test_migration_five_was_not_rewritten_for_semantic_cleanup(hermes_home: Path):
    """Profiles that already reached v5 must receive cleanup through v6."""
    body = " ".join(next(m for m in storage.MIGRATIONS if m.version == 5).statements)

    assert "model_proposed" not in body
    assert "WHERE durability = 'durable'" not in body


def test_migration_six_was_not_rewritten_for_privacy_cleanup(hermes_home: Path):
    """Profiles that already reached v6 must receive privacy cleanup via v7."""
    body = " ".join(next(m for m in storage.MIGRATIONS if m.version == 6).statements)

    assert "accessibility_needs" not in body
    assert "category = 'accessibility'" not in body


def test_a_v5_database_receives_lifecycle_and_provenance_cleanup(hermes_home: Path):
    _build_database_at(5)
    now = "2026-01-01T00:00:00+00:00"
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO learners"
            " (id, profile_id, principal_digest, platform, created_at, updated_at)"
            " VALUES ('L5', 'default', 'digest-5', 'telegram', ?, ?)",
            (now, now),
        )
        for candidate_id, durability in (("session-v5", "session"), ("short-v5", "short_term")):
            conn.execute(
                "INSERT INTO memory_candidates"
                " (id, learner_id, profile_id, track_id, category, statement, evidence_summary,"
                "  origin, evidence_count, confidence, durability, confirmation_state,"
                "  recommended_action, replaces, consent_reference, consented_need, expires_at,"
                "  created_at, updated_at)"
                " VALUES (?, 'L5', 'default', NULL, 'durable_preference', ?, 'Legacy row',"
                " 'repeated_evidence', 5, 'medium', ?, 'unconfirmed', 'add', NULL, NULL, NULL,"
                " NULL, ?, ?)",
                (candidate_id, candidate_id, durability, now, now),
            )
        conn.execute(
            "INSERT INTO memory_candidates"
            " (id, learner_id, profile_id, track_id, category, statement, evidence_summary,"
            "  origin, evidence_count, confidence, durability, confirmation_state,"
            "  recommended_action, replaces, consent_reference, consented_need, expires_at,"
            "  created_at, updated_at)"
            " VALUES ('authority-v5', 'L5', 'default', NULL, 'long_term_goal',"
            " 'Become a surgeon', 'Allegedly confirmed', 'confirmed_long_term_goal', NULL,"
            " 'medium', 'durable', 'learner_confirmed', 'add', NULL, NULL, NULL, NULL, ?, ?)",
            (now, now),
        )
        conn.commit()

    storage.initialize()

    with storage.connect() as conn:
        rows = conn.execute(
            "SELECT id, origin, confirmation_state FROM memory_candidates ORDER BY id"
        ).fetchall()
        version = storage.read_schema_version(conn)

    assert version == 9
    assert [dict(row) for row in rows] == [
        {
            "id": "authority-v5",
            "origin": "model_proposed",
            "confirmation_state": "unconfirmed",
        }
    ]


def test_a_failing_migration_six_rolls_back_cleanup(hermes_home: Path, monkeypatch):
    _build_database_at(5)
    now = "2026-01-01T00:00:00+00:00"
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO learners"
            " (id, profile_id, principal_digest, platform, created_at, updated_at)"
            " VALUES ('L5', 'default', 'digest-5', 'telegram', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO memory_candidates"
            " (id, learner_id, profile_id, track_id, category, statement, evidence_summary,"
            "  origin, evidence_count, confidence, durability, confirmation_state,"
            "  recommended_action, replaces, consent_reference, consented_need, expires_at,"
            "  created_at, updated_at)"
            " VALUES ('session-v5', 'L5', 'default', NULL, 'durable_preference', 'Legacy',"
            " 'Legacy row', 'repeated_evidence', 5, 'medium', 'session', 'unconfirmed',"
            " 'add', NULL, NULL, NULL, NULL, ?, ?)",
            (now, now),
        )
        conn.commit()

    real = list(storage.MIGRATIONS)
    monkeypatch.setattr(
        storage,
        "MIGRATIONS",
        [
            *real[:5],
            storage.Migration(version=6, statements=(*real[5].statements, "THIS IS NOT SQL")),
        ],
    )

    with pytest.raises(storage.MigrationError):
        storage.initialize()

    with storage.connect() as conn:
        version = storage.read_schema_version(conn)
        row = conn.execute(
            "SELECT durability FROM memory_candidates WHERE id = 'session-v5'"
        ).fetchone()

    assert version == 5
    assert row["durability"] == "session"


def _seed_accessibility_rows_under_v6() -> None:
    _build_database_at(6)
    now = "2026-01-01T00:00:00+00:00"
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO learners"
            " (id, profile_id, principal_digest, platform, created_at, updated_at)"
            " VALUES ('L6', 'default', 'digest-6', 'telegram', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO learning_contexts"
            " (id, learner_id, profile_id, scope, track_id, expires_at, created_at, updated_at)"
            " VALUES ('C6', 'L6', 'default', 'temporary', NULL, ?, ?, ?)",
            ("2027-01-01T00:00:00+00:00", now, now),
        )
        for value_id, field, value in (
            ("access-value", "accessibility_needs", '["captions"]'),
            ("goal-value", "goal", "Learn safely"),
        ):
            conn.execute(
                "INSERT INTO context_values"
                " (id, context_id, learner_id, profile_id, field, value, provenance,"
                "  confirmed, created_at, updated_at)"
                " VALUES (?, 'C6', 'L6', 'default', ?, ?, 'confirmed_track', 1, ?, ?)",
                (value_id, field, value, now, now),
            )
        for revision_id, field, value in (
            ("access-revision", "accessibility_needs", '["captions"]'),
            ("goal-revision", "goal", "Learn safely"),
        ):
            conn.execute(
                "INSERT INTO context_revisions"
                " (id, context_id, learner_id, profile_id, field, previous_value,"
                "  previous_provenance, new_value, provenance, change_reason, created_at)"
                " VALUES (?, 'C6', 'L6', 'default', ?, NULL, NULL, ?, 'confirmed_track',"
                " 'confirmed_track', ?)",
                (revision_id, field, value, now),
            )
        for candidate_id, category in (
            ("access-candidate", "accessibility"),
            ("goal-candidate", "long_term_goal"),
        ):
            conn.execute(
                "INSERT INTO memory_candidates"
                " (id, learner_id, profile_id, track_id, category, statement, evidence_summary,"
                "  origin, evidence_count, confidence, durability, confirmation_state,"
                "  recommended_action, replaces, consent_reference, consented_need, expires_at,"
                "  created_at, updated_at)"
                " VALUES (?, 'L6', 'default', NULL, ?, ?, 'Legacy row', 'model_proposed', NULL,"
                " 'medium', 'durable', 'unconfirmed', 'add', NULL, NULL, NULL, NULL, ?, ?)",
                (candidate_id, category, candidate_id, now, now),
            )
        conn.commit()


def test_a_v6_database_purges_only_legacy_accessibility_data(hermes_home: Path):
    _seed_accessibility_rows_under_v6()

    storage.initialize()

    with storage.connect() as conn:
        version = storage.read_schema_version(conn)
        values = [row["id"] for row in conn.execute("SELECT id FROM context_values ORDER BY id")]
        revisions = [
            row["id"] for row in conn.execute("SELECT id FROM context_revisions ORDER BY id")
        ]
        candidates = [
            row["id"] for row in conn.execute("SELECT id FROM memory_candidates ORDER BY id")
        ]
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()

    assert version == 9
    assert values == ["goal-value"]
    assert revisions == ["goal-revision"]
    assert candidates == ["goal-candidate"]
    assert foreign_keys == []


def test_a_failing_migration_seven_rolls_back_privacy_cleanup(hermes_home: Path, monkeypatch):
    _seed_accessibility_rows_under_v6()
    real = list(storage.MIGRATIONS)
    monkeypatch.setattr(
        storage,
        "MIGRATIONS",
        [
            *real[:6],
            storage.Migration(version=7, statements=(*real[6].statements, "THIS IS NOT SQL")),
        ],
    )

    with pytest.raises(storage.MigrationError):
        storage.initialize()

    with storage.connect() as conn:
        version = storage.read_schema_version(conn)
        sensitive_values = conn.execute(
            "SELECT COUNT(*) AS n FROM context_values WHERE field = 'accessibility_needs'"
        ).fetchone()["n"]
        sensitive_revisions = conn.execute(
            "SELECT COUNT(*) AS n FROM context_revisions WHERE field = 'accessibility_needs'"
        ).fetchone()["n"]
        sensitive_candidates = conn.execute(
            "SELECT COUNT(*) AS n FROM memory_candidates WHERE category = 'accessibility'"
        ).fetchone()["n"]

    assert version == 6
    assert sensitive_values == 1
    assert sensitive_revisions == 1
    assert sensitive_candidates == 1


def test_a_failing_migration_five_rolls_back_completely(hermes_home: Path, monkeypatch):
    candidate_id = _candidate_under_v4()

    real = list(storage.MIGRATIONS)
    monkeypatch.setattr(
        storage,
        "MIGRATIONS",
        [
            *real[:4],
            storage.Migration(version=5, statements=(*real[4].statements, "THIS IS NOT SQL")),
        ],
    )

    with pytest.raises(storage.MigrationError):
        storage.initialize()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == 4
        row = conn.execute(
            "SELECT statement FROM memory_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        objects = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}

    assert row is not None, "the candidate was lost by a rolled-back migration"
    assert not any(name.endswith("_v5") for name in objects), "a scratch table survived"


def test_two_callers_upgrading_a_v4_database_both_succeed(hermes_home: Path):
    _build_v4_database()

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def upgrade() -> None:
        try:
            barrier.wait(timeout=5)
            storage.initialize()
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=upgrade) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == [], f"concurrent upgrade failed: {errors}"
    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == storage.SCHEMA_VERSION


# ── Deleting a track no longer breaks its candidates ──────────────────────


def test_deleting_a_track_with_an_attached_candidate_succeeds(hermes_home: Path):
    """v4 raised ``NOT NULL constraint failed: memory_candidates.learner_id``.

    The composite key used ``ON DELETE SET NULL``, which tried to null the
    owner columns as well as ``track_id``.
    """
    track_id = service.save_context(principal=LEARNER, track={"name": "Doomed", "confirmed": True})[
        "outcome"
    ]["track"]["track_id"]
    service.save_context(
        principal=LEARNER,
        memory_candidates=[
            {
                "category": "durable_preference",
                "statement": "About the doomed track",
                "evidence_summary": "Said so",
                "origin": "repeated_evidence",
                "evidence_count": 5,
                "track_id": track_id,
            }
        ],
    )

    with storage.connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        conn.commit()

    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM memory_candidates").fetchone()["n"] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_an_unattached_candidate_survives_a_track_deletion(hermes_home: Path):
    """Only the track's own proposals go with it."""
    track_id = service.save_context(principal=LEARNER, track={"name": "Doomed", "confirmed": True})[
        "outcome"
    ]["track"]["track_id"]
    for extra in ({"track_id": track_id}, {}):
        service.save_context(
            principal=LEARNER,
            memory_candidates=[
                {
                    "category": "durable_preference",
                    "statement": f"Candidate {'with' if extra else 'without'} a track",
                    "evidence_summary": "Said so",
                    "origin": "repeated_evidence",
                    "evidence_count": 5,
                    **extra,
                }
            ],
        )

    with storage.connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        conn.commit()

    remaining = service.get_context(principal=LEARNER, include_memory_candidates=True)[
        "memory_candidates"
    ]
    assert [c["statement"] for c in remaining] == ["Candidate without a track"]
    assert remaining[0]["track_id"] is None


def test_deleting_a_learner_still_cascades_to_their_candidates(hermes_home: Path):
    service.save_context(
        principal=LEARNER,
        memory_candidates=[
            {
                "category": "durable_preference",
                "statement": "Anything",
                "evidence_summary": "Said so",
                "origin": "repeated_evidence",
                "evidence_count": 5,
            }
        ],
    )

    with storage.connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM learners")
        conn.commit()

    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM memory_candidates").fetchone()["n"] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_every_historical_version_upgrades_and_passes_a_foreign_key_check(
    hermes_home: Path, version: int
):
    _build_database_at(version)

    storage.initialize()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == storage.SCHEMA_VERSION
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
