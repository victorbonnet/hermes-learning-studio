"""SQLite persistence: connections, pragmas, and versioned migrations.

Design constraints that shaped this module:

- **No shared connection object.** ``sqlite3`` connections are not safe to
  hand between threads, and Hermes runs tool handlers from more than one.
  Every caller opens its own via :func:`connect`.
- **Migrations are all-or-nothing.** A migration is a *list of statements*,
  not a callback, and each statement is run with ``conn.execute`` inside one
  transaction. This is structural, not stylistic: ``executescript()`` issues
  a ``COMMIT`` before running its script, so DDL executed through it survives
  a later rollback and leaves a half-built schema behind. There is no way to
  express a migration here that escapes its transaction.
- **The write lock is taken before the version is read.** Two processes
  starting at once would otherwise both see version 0 and both try to create
  the schema.
- **An unknown database is never repaired.** If the file was written by a
  newer version of this plugin, the only safe action is to refuse. Deleting
  or "resetting" it would destroy a learner's record to make the code happy.
- **Ownership is a database constraint, not only a query convention.**
  Composite foreign keys make it impossible to store a row that claims one
  profile or learner while pointing at a parent belonging to another.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import LearningStudioConfig
from .paths import FILE_MODE, ensure_storage_root, storage_root

DATABASE_FILENAME = "learning-studio.sqlite3"


class StorageError(RuntimeError):
    """Base class for storage failures."""


class MigrationError(StorageError):
    """A migration failed and was rolled back."""


class IncompatibleSchemaError(StorageError):
    """The database on disk is newer than this code understands."""


@dataclass(frozen=True)
class Migration:
    """One schema version, expressed as ordered SQL statements.

    Statements rather than a callable so that every migration — including
    the ones tests construct — goes through the same transaction-preserving
    execution path. A migration cannot opt out of atomicity.
    """

    version: int
    statements: tuple[str, ...]


def database_path() -> Path:
    return storage_root() / DATABASE_FILENAME


# ── Schema ────────────────────────────────────────────────────────────────

# Conventions that run through every table:
#
# - ``profile_id`` and ``learner_id`` sit on every learner-owned row so
#   authorisation is a ``WHERE`` clause on the row itself rather than a join
#   the caller might forget.
# - Those columns are additionally tied to the parent by composite foreign
#   keys, so the denormalisation cannot drift: a track row claiming profile B
#   cannot reference a learner in profile A.
# - Identifiers are opaque generated tokens. Nothing keys on a username, a
#   display name, or any other label a learner can change.
#
# Migrations are append-only. Version 1 is frozen as the commit that first
# created it left it: a database that recorded version 1 will never re-run
# migration 1, so editing it cannot change what is already on disk — it only
# makes the recorded version a lie. Schema changes go in a new migration.

_MIGRATION_001 = (
    """
    CREATE TABLE schema_version (
        id          INTEGER PRIMARY KEY CHECK (id = 1),
        version     INTEGER NOT NULL,
        applied_at  TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE studio_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # A learner is one authenticated principal within one profile. The
    # principal digest is derived from host-supplied identity (platform +
    # sender ID), never from a tool argument.
    """
    CREATE TABLE learners (
        id                TEXT PRIMARY KEY,
        profile_id        TEXT NOT NULL,
        principal_digest  TEXT NOT NULL,
        platform          TEXT NOT NULL,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        UNIQUE (profile_id, principal_digest),
        -- Referenced by the composite foreign keys below.
        UNIQUE (id, profile_id)
    )
    """,
    # Sustained work toward a goal. Only ever created with explicit learner
    # confirmation; see service.save_context.
    """
    CREATE TABLE tracks (
        id           TEXT PRIMARY KEY,
        learner_id   TEXT NOT NULL,
        profile_id   TEXT NOT NULL,
        name         TEXT NOT NULL,
        status       TEXT NOT NULL
                     CHECK (status IN ('active', 'archived', 'withdrawn')),
        confirmed_at TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        UNIQUE (learner_id, name),
        UNIQUE (id, profile_id, learner_id),
        FOREIGN KEY (learner_id, profile_id)
            REFERENCES learners (id, profile_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX idx_tracks_owner ON tracks (profile_id, learner_id, status)",
    # A container for context values: either the learner's single temporary
    # context, or the durable context of one track.
    """
    CREATE TABLE learning_contexts (
        id          TEXT PRIMARY KEY,
        learner_id  TEXT NOT NULL,
        profile_id  TEXT NOT NULL,
        scope       TEXT NOT NULL CHECK (scope IN ('temporary', 'track')),
        track_id    TEXT,
        expires_at  TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        CHECK (
            (scope = 'track'     AND track_id IS NOT NULL AND expires_at IS NULL)
            OR
            (scope = 'temporary' AND track_id IS NULL)
        ),
        UNIQUE (id, profile_id, learner_id),
        FOREIGN KEY (learner_id, profile_id)
            REFERENCES learners (id, profile_id) ON DELETE CASCADE,
        FOREIGN KEY (track_id, profile_id, learner_id)
            REFERENCES tracks (id, profile_id, learner_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE UNIQUE INDEX idx_one_temporary_context_per_learner
        ON learning_contexts (learner_id) WHERE scope = 'temporary'
    """,
    """
    CREATE UNIQUE INDEX idx_one_context_per_track
        ON learning_contexts (track_id) WHERE scope = 'track'
    """,
    # Expiry sweeps scan this; without the index, profile-wide cleanup would
    # be a full table scan on every tool call.
    """
    CREATE INDEX idx_contexts_expiry
        ON learning_contexts (profile_id, expires_at) WHERE scope = 'temporary'
    """,
    # One row per (context, field, provenance). Keying on provenance as well
    # as field is what lets an explicit statement and a contradicting piece of
    # evidence coexist, so precedence can choose between them instead of the
    # later write silently destroying the earlier one.
    """
    CREATE TABLE context_values (
        id          TEXT PRIMARY KEY,
        context_id  TEXT NOT NULL,
        learner_id  TEXT NOT NULL,
        profile_id  TEXT NOT NULL,
        field       TEXT NOT NULL,
        value       TEXT NOT NULL,
        provenance  TEXT NOT NULL,
        confirmed   INTEGER NOT NULL CHECK (confirmed IN (0, 1)),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        UNIQUE (context_id, field, provenance),
        FOREIGN KEY (context_id, profile_id, learner_id)
            REFERENCES learning_contexts (id, profile_id, learner_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX idx_context_values_owner ON context_values (profile_id, learner_id)",
    # What changed, when, why, and what it replaced. Deliberately not a
    # transcript: the reason is a category, and the values are the structured
    # before/after, never the conversation that produced them.
    """
    CREATE TABLE context_revisions (
        id                  TEXT PRIMARY KEY,
        context_id          TEXT NOT NULL,
        learner_id          TEXT NOT NULL,
        profile_id          TEXT NOT NULL,
        field               TEXT NOT NULL,
        previous_value      TEXT,
        previous_provenance TEXT,
        new_value           TEXT NOT NULL,
        provenance          TEXT NOT NULL,
        change_reason       TEXT NOT NULL
                            CHECK (change_reason IN (
                                'explicit_request', 'explicit_correction',
                                'confirmed_track', 'evidence'
                            )),
        created_at          TEXT NOT NULL,
        FOREIGN KEY (context_id, profile_id, learner_id)
            REFERENCES learning_contexts (id, profile_id, learner_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_revisions_owner
        ON context_revisions (profile_id, learner_id, context_id, created_at)
    """,
    """
    CREATE TABLE objectives (
        id          TEXT PRIMARY KEY,
        track_id    TEXT NOT NULL,
        learner_id  TEXT NOT NULL,
        profile_id  TEXT NOT NULL,
        behavior    TEXT NOT NULL,
        condition   TEXT NOT NULL,
        standard    TEXT NOT NULL,
        status      TEXT NOT NULL
                    CHECK (status IN ('proposed', 'active', 'met', 'retired')),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        FOREIGN KEY (track_id, profile_id, learner_id)
            REFERENCES tracks (id, profile_id, learner_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX idx_objectives_owner ON objectives (profile_id, learner_id, track_id)",
    # Proposals for the agent to weigh. Not memory writes: this plugin has no
    # path to Hermes memory and never will have one. `origin`, `evidence_count`
    # and `consent_reference` are stored because a candidate that cannot be
    # audited later cannot responsibly be acted on later.
    """
    CREATE TABLE memory_candidates (
        id                 TEXT PRIMARY KEY,
        learner_id         TEXT NOT NULL,
        profile_id         TEXT NOT NULL,
        track_id           TEXT,
        category           TEXT NOT NULL,
        statement          TEXT NOT NULL,
        evidence_summary   TEXT NOT NULL,
        origin             TEXT NOT NULL,
        evidence_count     INTEGER,
        confidence         TEXT NOT NULL CHECK (confidence IN ('low', 'medium', 'high')),
        durability         TEXT NOT NULL
                           CHECK (durability IN ('session', 'short_term', 'durable')),
        confirmation_state TEXT NOT NULL
                           CHECK (confirmation_state IN (
                               'unconfirmed', 'learner_confirmed', 'learner_declined'
                           )),
        recommended_action TEXT NOT NULL
                           CHECK (recommended_action IN (
                               'add', 'replace', 'remove', 'no_action'
                           )),
        replaces           TEXT,
        consent_reference  TEXT,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL,
        FOREIGN KEY (learner_id, profile_id)
            REFERENCES learners (id, profile_id) ON DELETE CASCADE,
        FOREIGN KEY (track_id, profile_id, learner_id)
            REFERENCES tracks (id, profile_id, learner_id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX idx_candidates_owner ON memory_candidates (profile_id, learner_id)",
)


# Version 2 adds the consent binding for sensitive candidates.
#
# The deletion comes first, and is the point of the migration rather than
# housekeeping. Version 1 accepted an accessibility candidate on the strength
# of *any* consent statement, so a v1 database can hold rows where "remember I
# need captions" was taken as authorisation to record a diagnosis. Those rows
# have no verifiable consent scope, and there is no honest way to give them
# one: parsing ``consent_reference`` would be inventing consent, and matching
# prose would be guessing about someone's health. Keeping them would carry the
# defect forward into a schema that promises every sensitive row is bound to a
# need the learner named.
#
# So they are removed. Non-sensitive candidates are untouched and simply get a
# NULL binding, which is what "this row is not sensitive" means here.
_MIGRATION_002 = (
    "DELETE FROM memory_candidates WHERE category = 'accessibility'",
    "ALTER TABLE memory_candidates ADD COLUMN consented_need TEXT",
)


# Version 3 adds stored learning experiences.
#
# The split across two tables is the schema half of the manifest's
# visible/hidden boundary. ``experience_components`` holds only what a learner
# may see; every answer key, rubric, scoring rule, hint, per-option feedback,
# branch and evaluator note lives in ``experience_component_evaluations``. A
# projection that forgets to exclude a column therefore cannot leak an answer,
# because the learner-facing table does not contain one — and a future reader
# that only ever joins the first table is safe by default.
#
# ``component_key`` is the author's own label for a component. It is not a
# primary key and never an authorisation boundary: rows key on generated
# opaque ids, so nothing a model writes can address a row.
_MIGRATION_003 = (
    # Objectives gained composite uniqueness so an experience can reference one
    # through a composite foreign key. Additive: an index, not a table change.
    """
    CREATE UNIQUE INDEX idx_objectives_identity
        ON objectives (id, profile_id, learner_id)
    """,
    """
    CREATE TABLE experiences (
        id                        TEXT PRIMARY KEY,
        learner_id                TEXT NOT NULL,
        profile_id                TEXT NOT NULL,
        track_id                  TEXT,
        objective_id              TEXT,
        manifest_schema_version   INTEGER NOT NULL,
        title                     TEXT    NOT NULL,
        objective_behavior        TEXT    NOT NULL,
        objective_condition       TEXT    NOT NULL,
        objective_standard        TEXT    NOT NULL,
        instructions              TEXT    NOT NULL,
        ui_locale                 TEXT    NOT NULL,
        content_locale            TEXT,
        expected_duration_minutes INTEGER NOT NULL
                                  CHECK (expected_duration_minutes BETWEEN 1 AND 240),
        difficulty                TEXT    NOT NULL
                                  CHECK (difficulty IN (
                                      'introductory', 'intermediate', 'advanced', 'expert'
                                  )),
        accessibility             TEXT    NOT NULL,
        source_references         TEXT    NOT NULL,
        delivery                  TEXT    NOT NULL,
        component_count           INTEGER NOT NULL CHECK (component_count > 0),
        created_at                TEXT    NOT NULL,
        updated_at                TEXT    NOT NULL,
        UNIQUE (id, profile_id, learner_id),
        FOREIGN KEY (learner_id, profile_id)
            REFERENCES learners (id, profile_id) ON DELETE CASCADE,
        FOREIGN KEY (track_id, profile_id, learner_id)
            REFERENCES tracks (id, profile_id, learner_id) ON DELETE CASCADE,
        FOREIGN KEY (objective_id, profile_id, learner_id)
            REFERENCES objectives (id, profile_id, learner_id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX idx_experiences_owner ON experiences (profile_id, learner_id, created_at)",
    """
    CREATE TABLE experience_components (
        id             TEXT    PRIMARY KEY,
        experience_id  TEXT    NOT NULL,
        learner_id     TEXT    NOT NULL,
        profile_id     TEXT    NOT NULL,
        position       INTEGER NOT NULL CHECK (position > 0),
        component_key  TEXT    NOT NULL,
        component_type TEXT    NOT NULL,
        learner_payload TEXT   NOT NULL,
        created_at     TEXT    NOT NULL,
        UNIQUE (experience_id, position),
        UNIQUE (experience_id, component_key),
        UNIQUE (id, profile_id, learner_id),
        FOREIGN KEY (experience_id, profile_id, learner_id)
            REFERENCES experiences (id, profile_id, learner_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_experience_components_owner
        ON experience_components (profile_id, learner_id, experience_id, position)
    """,
    """
    CREATE TABLE experience_component_evaluations (
        component_id  TEXT PRIMARY KEY,
        experience_id TEXT NOT NULL,
        learner_id    TEXT NOT NULL,
        profile_id    TEXT NOT NULL,
        evaluation    TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        FOREIGN KEY (component_id, profile_id, learner_id)
            REFERENCES experience_components (id, profile_id, learner_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_experience_evaluations_owner
        ON experience_component_evaluations (profile_id, learner_id, experience_id)
    """,
)


# Version 4 rebuilds the experience tables so that three relationships the v3
# schema only *claimed* are actually enforced by SQLite:
#
# 1. **An objective belongs to a track.** The v3 foreign key omitted
#    ``track_id``, so an experience attached to track B could reference an
#    objective from track A as long as both belonged to the same learner. The
#    new key includes the track, which makes that row unstorable.
# 2. **Deleting an objective works.** The v3 key used ``ON DELETE SET NULL``
#    over a composite that includes ``profile_id`` and ``learner_id``, both
#    ``NOT NULL`` — so deleting any objective raised
#    ``NOT NULL constraint failed``. The action is now ``CASCADE``: an
#    objective is never deleted by this plugin (it is retired), and the only
#    thing that deletes one is a track deletion, which already removes the
#    same experiences by the track key. Cascade therefore agrees with what
#    happens anyway, and — unlike RESTRICT — cannot deadlock a track deletion
#    against its own children.
# 3. **An evaluator row belongs to its component's experience.** The v3 key
#    constrained only ``component_id``, so an evaluation could name a
#    different, or nonexistent, experience. The key now spans component,
#    experience, profile and learner together.
#
# The rebuild is expressed as ordinary statements — no ``executescript`` — so
# it runs inside the caller's transaction and rolls back whole. The new tables
# are built and populated first, the old ones dropped afterwards (children
# before parents, so the implicit ``DELETE`` a ``DROP`` performs has nothing
# left to cascade into), and the renames then fix up the references between
# them.
#
# Two deterministic repairs, because a v3 database may already hold rows the
# new constraints forbid:
#
# - an experience whose objective belongs to another track, or to no surviving
#   objective at all, keeps the experience and loses the objective link;
# - an evaluator row whose component is missing, or whose experience does not
#   match that component's, is dropped. It cannot be attributed to anything,
#   and guessing an owner for evaluator data is worse than losing it.
_MIGRATION_004 = (
    """
    CREATE UNIQUE INDEX idx_objectives_track_identity
        ON objectives (id, track_id, profile_id, learner_id)
    """,
    """
    CREATE TABLE experiences_v4 (
        id                        TEXT PRIMARY KEY,
        learner_id                TEXT NOT NULL,
        profile_id                TEXT NOT NULL,
        track_id                  TEXT,
        objective_id              TEXT,
        manifest_schema_version   INTEGER NOT NULL,
        title                     TEXT    NOT NULL,
        objective_behavior        TEXT    NOT NULL,
        objective_condition       TEXT    NOT NULL,
        objective_standard        TEXT    NOT NULL,
        instructions              TEXT    NOT NULL,
        ui_locale                 TEXT    NOT NULL,
        content_locale            TEXT,
        expected_duration_minutes INTEGER NOT NULL
                                  CHECK (expected_duration_minutes BETWEEN 1 AND 240),
        difficulty                TEXT    NOT NULL
                                  CHECK (difficulty IN (
                                      'introductory', 'intermediate', 'advanced', 'expert'
                                  )),
        accessibility             TEXT    NOT NULL,
        source_references         TEXT    NOT NULL,
        delivery                  TEXT    NOT NULL,
        component_count           INTEGER NOT NULL CHECK (component_count > 0),
        created_at                TEXT    NOT NULL,
        updated_at                TEXT    NOT NULL,
        -- An objective may only be named together with the track that owns it.
        CHECK (objective_id IS NULL OR track_id IS NOT NULL),
        UNIQUE (id, profile_id, learner_id),
        FOREIGN KEY (learner_id, profile_id)
            REFERENCES learners (id, profile_id) ON DELETE CASCADE,
        FOREIGN KEY (track_id, profile_id, learner_id)
            REFERENCES tracks (id, profile_id, learner_id) ON DELETE CASCADE,
        FOREIGN KEY (objective_id, track_id, profile_id, learner_id)
            REFERENCES objectives (id, track_id, profile_id, learner_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE experience_components_v4 (
        id              TEXT    PRIMARY KEY,
        experience_id   TEXT    NOT NULL,
        learner_id      TEXT    NOT NULL,
        profile_id      TEXT    NOT NULL,
        position        INTEGER NOT NULL CHECK (position > 0),
        component_key   TEXT    NOT NULL,
        component_type  TEXT    NOT NULL,
        learner_payload TEXT    NOT NULL,
        created_at      TEXT    NOT NULL,
        UNIQUE (experience_id, position),
        UNIQUE (experience_id, component_key),
        UNIQUE (id, profile_id, learner_id),
        -- Referenced by the evaluator table's four-column key below.
        UNIQUE (id, experience_id, profile_id, learner_id),
        FOREIGN KEY (experience_id, profile_id, learner_id)
            REFERENCES experiences_v4 (id, profile_id, learner_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE experience_component_evaluations_v4 (
        component_id  TEXT PRIMARY KEY,
        experience_id TEXT NOT NULL,
        learner_id    TEXT NOT NULL,
        profile_id    TEXT NOT NULL,
        evaluation    TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        FOREIGN KEY (component_id, experience_id, profile_id, learner_id)
            REFERENCES experience_components_v4 (id, experience_id, profile_id, learner_id)
            ON DELETE CASCADE
    )
    """,
    """
    INSERT INTO experiences_v4
        SELECT
            e.id, e.learner_id, e.profile_id, e.track_id,
            CASE
                WHEN e.objective_id IS NULL THEN NULL
                WHEN EXISTS (
                    SELECT 1 FROM objectives o
                     WHERE o.id = e.objective_id
                       AND o.track_id = e.track_id
                       AND o.profile_id = e.profile_id
                       AND o.learner_id = e.learner_id
                ) THEN e.objective_id
                ELSE NULL
            END,
            e.manifest_schema_version, e.title, e.objective_behavior, e.objective_condition,
            e.objective_standard, e.instructions, e.ui_locale, e.content_locale,
            e.expected_duration_minutes, e.difficulty, e.accessibility, e.source_references,
            e.delivery, e.component_count, e.created_at, e.updated_at
        FROM experiences e
    """,
    """
    INSERT INTO experience_components_v4
        SELECT c.* FROM experience_components c
         WHERE EXISTS (SELECT 1 FROM experiences_v4 e WHERE e.id = c.experience_id)
    """,
    """
    INSERT INTO experience_component_evaluations_v4
        SELECT v.* FROM experience_component_evaluations v
         JOIN experience_components_v4 c
           ON c.id = v.component_id
          AND c.experience_id = v.experience_id
          AND c.profile_id = v.profile_id
          AND c.learner_id = v.learner_id
    """,
    "DROP TABLE experience_component_evaluations",
    "DROP TABLE experience_components",
    "DROP TABLE experiences",
    "ALTER TABLE experiences_v4 RENAME TO experiences",
    "ALTER TABLE experience_components_v4 RENAME TO experience_components",
    "ALTER TABLE experience_component_evaluations_v4 RENAME TO experience_component_evaluations",
    "CREATE INDEX idx_experiences_owner ON experiences (profile_id, learner_id, created_at)",
    """
    CREATE INDEX idx_experience_components_owner
        ON experience_components (profile_id, learner_id, experience_id, position)
    """,
    """
    CREATE INDEX idx_experience_evaluations_owner
        ON experience_component_evaluations (profile_id, learner_id, experience_id)
    """,
)


# Version 5 rebuilds ``memory_candidates`` for two reasons.
#
# 1. **Track deletion was impossible.** The track foreign key used
#    ``ON DELETE SET NULL`` over a composite that includes ``profile_id`` and
#    ``learner_id``, both ``NOT NULL`` — so deleting a track that had a
#    candidate attached raised ``NOT NULL constraint failed:
#    memory_candidates.learner_id``. The same defect the experiences table had
#    at v3, in a table the v4 rebuild did not touch.
#
#    The action is now ``CASCADE``. A candidate that names a track is a
#    proposal *about that track's* work, so removing the track removes it;
#    candidates with no track are untouched, because the child key is NULL and
#    a composite foreign key with a NULL column is satisfied trivially.
#
# 2. **``short_term`` had no expiry.** A candidate labelled short-term was
#    stored forever, which made the label a lie. ``expires_at`` is nullable —
#    ``NULL`` means durable — and the service sets it for short-term rows and
#    sweeps them on read.
#
# Rebuilt rather than altered because a foreign-key action cannot be changed
# with ``ALTER TABLE``. Same shape as migration 4: build, copy, drop, rename,
# all as ordinary statements inside the caller's transaction.
_MIGRATION_005 = (
    """
    CREATE TABLE memory_candidates_v5 (
        id                 TEXT PRIMARY KEY,
        learner_id         TEXT NOT NULL,
        profile_id         TEXT NOT NULL,
        track_id           TEXT,
        category           TEXT NOT NULL,
        statement          TEXT NOT NULL,
        evidence_summary   TEXT NOT NULL,
        origin             TEXT NOT NULL,
        evidence_count     INTEGER,
        confidence         TEXT NOT NULL CHECK (confidence IN ('low', 'medium', 'high')),
        durability         TEXT NOT NULL
                           CHECK (durability IN ('session', 'short_term', 'durable')),
        confirmation_state TEXT NOT NULL
                           CHECK (confirmation_state IN (
                               'unconfirmed', 'learner_confirmed', 'learner_declined'
                           )),
        recommended_action TEXT NOT NULL
                           CHECK (recommended_action IN (
                               'add', 'replace', 'remove', 'no_action'
                           )),
        replaces           TEXT,
        consent_reference  TEXT,
        consented_need     TEXT,
        expires_at         TEXT,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL,
        FOREIGN KEY (learner_id, profile_id)
            REFERENCES learners (id, profile_id) ON DELETE CASCADE,
        FOREIGN KEY (track_id, profile_id, learner_id)
            REFERENCES tracks (id, profile_id, learner_id) ON DELETE CASCADE
    )
    """,
    """
    INSERT INTO memory_candidates_v5
        SELECT id, learner_id, profile_id, track_id, category, statement, evidence_summary,
               origin, evidence_count, confidence, durability, confirmation_state,
               recommended_action, replaces, consent_reference, consented_need,
               NULL, created_at, updated_at
          FROM memory_candidates
    """,
    "DROP TABLE memory_candidates",
    "ALTER TABLE memory_candidates_v5 RENAME TO memory_candidates",
    "CREATE INDEX idx_candidates_owner ON memory_candidates (profile_id, learner_id)",
    """
    CREATE INDEX idx_candidates_expiry
        ON memory_candidates (profile_id, expires_at) WHERE expires_at IS NOT NULL
    """,
)


# Migration 6 changes lifecycle and provenance *semantics*, not table shape.
# Migration 5 may already have run on a database created while this PR was
# under review, so it remains byte-for-byte unchanged and cleanup is appended:
#
# - legacy ``session`` and ``short_term`` rows have no trustworthy session id
#   or expiry timestamp and are removed rather than made permanent;
# - legacy learner-authority claims are downgraded because no host-backed
#   confirmation event existed when they were stored.
_MIGRATION_006 = (
    "DELETE FROM memory_candidates WHERE durability IN ('session', 'short_term')",
    """
    UPDATE memory_candidates
       SET origin = 'model_proposed'
     WHERE origin IN (
         'explicit_durable_preference',
         'confirmed_long_term_goal',
         'explicit_correction',
         'explicit_withdrawal'
     )
    """,
    """
    UPDATE memory_candidates
       SET confirmation_state = 'unconfirmed'
     WHERE confirmation_state IN ('learner_confirmed', 'learner_declined')
    """,
)


# Migration 7 enforces the unconditional session-only accessibility policy on
# databases that may already have reached v6. Older releases could persist the
# same sensitive fact in current values, revision history, or candidates. None
# has host-backed consent, so all three representations are purged together;
# unrelated learning context and candidates remain intact.
_MIGRATION_007 = (
    "DELETE FROM context_revisions WHERE field = 'accessibility_needs'",
    "DELETE FROM context_values WHERE field = 'accessibility_needs'",
    "DELETE FROM memory_candidates WHERE category = 'accessibility'",
)


# Migration 8 adds profile-managed educational image assets. Source paths are
# intentionally absent: provenance and generation prompts are retained for
# audit server-side, while the local file that happened to produce the bytes is
# neither durable identity nor metadata a client should ever receive.
_MIGRATION_008 = (
    """
    CREATE TABLE managed_assets (
        id                TEXT    PRIMARY KEY,
        learner_id        TEXT    NOT NULL,
        profile_id        TEXT    NOT NULL,
        track_id          TEXT,
        scope_key         TEXT    NOT NULL,
        title             TEXT    NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 200),
        alt_text          TEXT,
        decorative        INTEGER NOT NULL CHECK (decorative IN (0, 1)),
        provenance        TEXT    NOT NULL CHECK (provenance IN (
            'host_image_generation', 'learner_provided', 'operator_selected'
        )),
        generation_prompt TEXT    CHECK (
            generation_prompt IS NULL
            OR length(trim(generation_prompt)) BETWEEN 1 AND 4000
        ),
        sha256            TEXT    NOT NULL CHECK (
            length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
        ),
        mime_type         TEXT    NOT NULL CHECK (mime_type IN (
            'image/png', 'image/jpeg', 'image/webp'
        )),
        byte_size         INTEGER NOT NULL CHECK (byte_size > 0),
        width             INTEGER NOT NULL CHECK (width > 0),
        height            INTEGER NOT NULL CHECK (height > 0),
        storage_name      TEXT    NOT NULL UNIQUE CHECK (
            length(storage_name) BETWEEN 1 AND 100
            AND instr(storage_name, '/') = 0
            AND instr(storage_name, char(92)) = 0
        ),
        created_at        TEXT    NOT NULL,
        updated_at        TEXT    NOT NULL,
        CHECK (
            (track_id IS NULL AND scope_key = '')
            OR (track_id IS NOT NULL AND scope_key = track_id)
        ),
        CHECK (
            (decorative = 1 AND alt_text IS NULL)
            OR (
                decorative = 0 AND alt_text IS NOT NULL
                AND length(trim(alt_text)) BETWEEN 1 AND 1000
            )
        ),
        UNIQUE (id, profile_id, learner_id),
        UNIQUE (profile_id, learner_id, scope_key, sha256),
        FOREIGN KEY (learner_id, profile_id)
            REFERENCES learners (id, profile_id) ON DELETE CASCADE,
        FOREIGN KEY (track_id, profile_id, learner_id)
            REFERENCES tracks (id, profile_id, learner_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_managed_assets_owner
        ON managed_assets (profile_id, learner_id, scope_key, created_at)
    """,
)


# Version 9 records alias provenance for every evaluator row outside the mutable
# evaluator JSON. Scheme 3 rows carry a digest binding the complete alias ->
# canonical mapping to the component, learner projection, owner and experience.
# Older rows receive explicit migration markers instead: scheme 1/unversioned
# keeps its documented compatibility semantics, scheme 2 remains fail-closed,
# and genuinely pre-alias evaluator rows are marked canonical. Requiring that
# separate marker prevents a current record from being downgraded by stripping
# its alias fields.
_MIGRATION_009 = (
    """
    CREATE TABLE experience_component_alias_bindings (
        component_id   TEXT PRIMARY KEY,
        experience_id  TEXT NOT NULL,
        learner_id     TEXT NOT NULL,
        profile_id     TEXT NOT NULL,
        binding_scheme INTEGER NOT NULL CHECK (binding_scheme BETWEEN 0 AND 3),
        binding_digest TEXT,
        created_at     TEXT NOT NULL,
        CHECK (
            (
                binding_scheme = 3
                AND binding_digest IS NOT NULL
                AND length(binding_digest) = 64
                AND binding_digest NOT GLOB '*[^0-9a-f]*'
            )
            OR (
                binding_scheme IN (0, 1, 2)
                AND binding_digest IS NULL
            )
        ),
        FOREIGN KEY (component_id, experience_id, profile_id, learner_id)
            REFERENCES experience_components (id, experience_id, profile_id, learner_id)
            ON DELETE CASCADE
    )
    """,
    """
    INSERT INTO experience_component_alias_bindings
        (component_id, experience_id, learner_id, profile_id,
         binding_scheme, binding_digest, created_at)
    SELECT
        e.component_id,
        e.experience_id,
        e.learner_id,
        e.profile_id,
        CASE
            WHEN json_valid(e.evaluation) = 0 THEN 0
            WHEN json_type(e.evaluation, '$.aliases') = 'object' THEN
                CASE
                    WHEN json_type(e.evaluation, '$.alias_scheme') = 'integer'
                         AND json_extract(e.evaluation, '$.alias_scheme') = 2
                    THEN 2
                    ELSE 1
                END
            ELSE 0
        END,
        NULL,
        e.created_at
    FROM experience_component_evaluations AS e
    """,
    """
    CREATE INDEX idx_experience_alias_bindings_owner
        ON experience_component_alias_bindings
           (profile_id, learner_id, experience_id)
    """,
)


# Version 10 adds the evaluation runtime: durable attempts, per-component
# results, spaced-repetition review state, a structured misconception bank,
# and one opt-in preference flag.
#
# Four tables, and every one of them keeps the same promises the rest of the
# schema already makes:
#
# - **No learner-owned row without an ownership column.** Every table here
#   carries ``profile_id`` and ``learner_id``, and every foreign key includes
#   them, so a query that forgets a ``WHERE`` clause fails loudly rather than
#   returning someone else's data.
# - **Erasure is one deletion away.** Every table's ownership chain resolves
#   back to ``learners`` through ``ON DELETE CASCADE`` — ``attempts`` and
#   ``attempt_components`` through ``experiences``, ``review_state`` and
#   ``misconceptions`` through ``objectives`` when an objective is named and
#   directly otherwise — so ``DELETE FROM learners WHERE id = ? AND
#   profile_id = ?`` removes every row this migration adds, in the caller's
#   one transaction, the same way it already removes tracks, contexts,
#   experiences and managed assets.
# - **No raw learner response text.** ``attempt_components`` stores a mark —
#   ``graded``, ``correct``, ``score``, ``max_score`` — and never the
#   submitted value that produced it. What a self-report actually said
#   (a flashcard's self-rating, a confidence rating) is a closed vocabulary
#   or a bounded integer, not free text, and even that is not retained once
#   it has fed ``review_state`` — see ``learning_studio.service.record_attempt``.
_MIGRATION_010 = (
    """
    CREATE TABLE attempts (
        id                    TEXT    PRIMARY KEY,
        profile_id            TEXT    NOT NULL,
        learner_id            TEXT    NOT NULL,
        experience_id         TEXT    NOT NULL,
        track_id              TEXT,
        objective_id          TEXT,
        component_count       INTEGER NOT NULL CHECK (component_count >= 0),
        graded_count          INTEGER NOT NULL CHECK (graded_count >= 0),
        correct_count         INTEGER NOT NULL CHECK (correct_count >= 0),
        overall_score         REAL,
        overall_max_score     REAL,
        started_at            TEXT    NOT NULL,
        completed_at          TEXT    NOT NULL,
        created_at            TEXT    NOT NULL,
        UNIQUE (id, profile_id, learner_id),
        FOREIGN KEY (learner_id, profile_id)
            REFERENCES learners (id, profile_id) ON DELETE CASCADE,
        FOREIGN KEY (experience_id, profile_id, learner_id)
            REFERENCES experiences (id, profile_id, learner_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX idx_attempts_owner ON attempts (profile_id, learner_id, completed_at)",
    "CREATE INDEX idx_attempts_objective ON attempts (profile_id, learner_id, objective_id)",
    """
    CREATE TABLE attempt_components (
        id             TEXT    PRIMARY KEY,
        attempt_id     TEXT    NOT NULL,
        profile_id     TEXT    NOT NULL,
        learner_id     TEXT    NOT NULL,
        experience_id  TEXT    NOT NULL,
        component_id   TEXT    NOT NULL,
        component_type TEXT    NOT NULL,
        objective_id   TEXT,
        graded         INTEGER NOT NULL CHECK (graded IN (0, 1)),
        correct        INTEGER CHECK (correct IN (0, 1)),
        score          REAL,
        max_score      REAL,
        created_at     TEXT    NOT NULL,
        UNIQUE (attempt_id, component_id),
        FOREIGN KEY (attempt_id, profile_id, learner_id)
            REFERENCES attempts (id, profile_id, learner_id) ON DELETE CASCADE,
        FOREIGN KEY (component_id, experience_id, profile_id, learner_id)
            REFERENCES experience_components (id, experience_id, profile_id, learner_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_attempt_components_owner
        ON attempt_components (profile_id, learner_id, attempt_id)
    """,
    """
    CREATE INDEX idx_attempt_components_objective
        ON attempt_components (profile_id, learner_id, objective_id, component_type)
    """,
    # One row per (learner, objective): the spaced-repetition state machine's
    # own memory, distinct from — and never derived from — Hermes memory.
    """
    CREATE TABLE review_state (
        id              TEXT    PRIMARY KEY,
        profile_id      TEXT    NOT NULL,
        learner_id      TEXT    NOT NULL,
        objective_id    TEXT    NOT NULL,
        interval_days   INTEGER NOT NULL CHECK (interval_days >= 1),
        ease_factor     REAL    NOT NULL CHECK (ease_factor >= 1.3),
        repetitions     INTEGER NOT NULL CHECK (repetitions >= 0),
        last_quality    INTEGER NOT NULL CHECK (last_quality BETWEEN 0 AND 5),
        last_reviewed_at TEXT   NOT NULL,
        next_review_at  TEXT    NOT NULL,
        created_at      TEXT    NOT NULL,
        updated_at      TEXT    NOT NULL,
        UNIQUE (profile_id, learner_id, objective_id),
        FOREIGN KEY (objective_id, profile_id, learner_id)
            REFERENCES objectives (id, profile_id, learner_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_review_state_due
        ON review_state (profile_id, learner_id, next_review_at)
    """,
    # Structured, never free text: a category is (objective, component type),
    # counted. "ser/estar confusion" is the kind of label an agent derives
    # from that pair and the objective's own wording when it reads this back
    # — never a string stored here.
    """
    CREATE TABLE misconceptions (
        id             TEXT    PRIMARY KEY,
        profile_id     TEXT    NOT NULL,
        learner_id     TEXT    NOT NULL,
        objective_id   TEXT,
        component_type TEXT    NOT NULL,
        occurrences    INTEGER NOT NULL CHECK (occurrences >= 1),
        last_seen_at   TEXT    NOT NULL,
        created_at     TEXT    NOT NULL,
        updated_at     TEXT    NOT NULL,
        FOREIGN KEY (learner_id, profile_id)
            REFERENCES learners (id, profile_id) ON DELETE CASCADE,
        FOREIGN KEY (objective_id, profile_id, learner_id)
            REFERENCES objectives (id, profile_id, learner_id) ON DELETE CASCADE
    )
    """,
    # A NULL objective_id means "not attributed to one objective"; the
    # expression index treats every such row as sharing one identity per
    # (learner, component_type) rather than as distinct, which plain
    # ``UNIQUE`` cannot do since SQL does not equate two NULLs.
    """
    CREATE UNIQUE INDEX idx_misconceptions_identity
        ON misconceptions (profile_id, learner_id, COALESCE(objective_id, ''), component_type)
    """,
    "CREATE INDEX idx_misconceptions_owner ON misconceptions (profile_id, learner_id)",
    # One flag, defaulting closed. The plugin never sends anything on its
    # own; this is only ever read by the operator's own cron-triggered check,
    # documented in the README, and only ever set by
    # ``learning_studio_set_review_reminders`` on the learner's own say-so.
    """
    CREATE TABLE learner_preferences (
        learner_id                 TEXT    PRIMARY KEY,
        profile_id                 TEXT    NOT NULL,
        review_reminders_enabled   INTEGER NOT NULL DEFAULT 0
                                   CHECK (review_reminders_enabled IN (0, 1)),
        created_at                 TEXT    NOT NULL,
        updated_at                 TEXT    NOT NULL,
        FOREIGN KEY (learner_id, profile_id)
            REFERENCES learners (id, profile_id) ON DELETE CASCADE
    )
    """,
)


#: Ordered, contiguous from 1. The list order is the application order.
MIGRATIONS: list[Migration] = [
    Migration(version=1, statements=_MIGRATION_001),
    Migration(version=2, statements=_MIGRATION_002),
    Migration(version=3, statements=_MIGRATION_003),
    Migration(version=4, statements=_MIGRATION_004),
    Migration(version=5, statements=_MIGRATION_005),
    Migration(version=6, statements=_MIGRATION_006),
    Migration(version=7, statements=_MIGRATION_007),
    Migration(version=8, statements=_MIGRATION_008),
    Migration(version=9, statements=_MIGRATION_009),
    Migration(version=10, statements=_MIGRATION_010),
]

SCHEMA_VERSION = MIGRATIONS[-1].version


# ── Connections ───────────────────────────────────────────────────────────


@contextmanager
def connect(config: LearningStudioConfig | None = None) -> Iterator[sqlite3.Connection]:
    """Open a configured connection to the Learning Studio database.

    Foreign keys are enabled per-connection because SQLite defaults them
    *off* — a schema full of ``REFERENCES`` clauses enforces nothing without
    this pragma on every single connection.
    """
    config = config or LearningStudioConfig()
    ensure_storage_root()
    path = database_path()
    existed = path.exists()

    conn = sqlite3.connect(
        path,
        timeout=config.busy_timeout_ms / 1000,
        isolation_level=None,  # explicit transactions; see `transaction()`
    )
    try:
        if not existed:
            _restrict_database_file(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {config.busy_timeout_ms}")
        _set_journal_mode(conn, config.journal_mode)
        yield conn
    finally:
        conn.close()


def _set_journal_mode(conn: sqlite3.Connection, requested: str) -> None:
    """Apply the journal mode, falling back if the filesystem refuses WAL.

    WAL needs shared memory and real file locking; network mounts and some
    container filesystems provide neither. SQLite reports this by returning
    the mode it actually used, so the return value is checked rather than
    assumed.
    """
    try:
        actual = conn.execute(f"PRAGMA journal_mode = {requested}").fetchone()[0]
    except sqlite3.DatabaseError:
        actual = None
    if requested == "wal" and (actual or "").lower() != "wal":
        conn.execute("PRAGMA journal_mode = DELETE")


def _restrict_database_file(path: Path) -> None:
    try:
        path.touch(mode=FILE_MODE, exist_ok=True)
        path.chmod(FILE_MODE)
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pass


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside one transaction, rolling back on any exception.

    Multi-step writes — a track plus its context plus its revisions — must
    not be able to half-succeed, or a learner ends up with a confirmed track
    that has no context, or context attributed to a track that was never
    created.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


# ── Migrations ────────────────────────────────────────────────────────────


def read_schema_version(conn: sqlite3.Connection) -> int:
    """Return the schema version on disk, or 0 for a fresh database."""
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if table is None:
        return 0
    row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    return int(row["version"]) if row else 0


def _refuse_newer_database() -> None:
    """Read the version through a read-only handle, before anything is set up.

    This runs *before* :func:`connect`, and that ordering is the whole point.
    ``connect`` applies the configured journal mode, which is persistent: a
    database written by a newer version of the plugin used to be converted to
    WAL — new files on disk, changed bytes — and only then refused. "The
    database has been left untouched" has to be true, so the compatibility
    question is asked by a handle that cannot write.

    A missing file is not an error; it is a first run.
    """
    root = storage_root()
    path = root / DATABASE_FILENAME
    if not path.exists():
        return

    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:  # pragma: no cover - platform or permission dependent
        # No read-only handle available. Refusing here would make an ordinary
        # database unusable, so fall through: `initialize` checks the version
        # again on the normal connection.
        return
    try:
        conn.row_factory = sqlite3.Row
        _check_version(read_schema_version(conn))
    except sqlite3.DatabaseError:  # pragma: no cover - unreadable or not a database
        return
    finally:
        conn.close()


def initialize(config: LearningStudioConfig | None = None) -> None:
    """Create or migrate the database. Safe to call concurrently and repeatedly.

    The version is read three times, and each read earns its place. The first
    is read-only and happens before any connection is configured, so an
    unsupported newer database is refused without being touched. The second is
    an unlocked fast path for the overwhelmingly common case of an up-to-date
    database. The third happens *after* ``BEGIN IMMEDIATE`` has taken the write
    lock, because between the two another process may have done the work — and
    acting on the stale answer would mean trying to create tables that now
    exist.

    Raises :class:`IncompatibleSchemaError` if the database is newer than
    this code, and :class:`MigrationError` if a migration fails, in which
    case nothing from the upgrade has been applied.
    """
    config = config or LearningStudioConfig()
    _refuse_newer_database()
    with connect(config) as conn:
        if _check_version(read_schema_version(conn)):
            return

        conn.execute("BEGIN IMMEDIATE")
        try:
            current = read_schema_version(conn)
            if _check_version(current):
                conn.rollback()
                return

            pending = [m for m in MIGRATIONS if m.version > current]
            for migration in pending:
                _apply_locked(conn, migration)
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()


def _check_version(current: int) -> bool:
    """Return True when the schema is already current; raise if it is newer."""
    if current > SCHEMA_VERSION:
        raise IncompatibleSchemaError(
            f"The Learning Studio database is at schema version {current}, but this "
            f"version of the plugin understands {SCHEMA_VERSION}. Upgrade the plugin. "
            "The database has been left untouched."
        )
    return current == SCHEMA_VERSION


def _apply_locked(conn: sqlite3.Connection, migration: Migration) -> None:
    """Apply one migration inside the caller's already-open transaction.

    Every statement goes through ``conn.execute``. ``executescript`` would
    commit first and take the DDL out of the transaction, which is the whole
    reason migrations are stored as statement lists.
    """
    try:
        for statement in migration.statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_version (id, version, applied_at) VALUES (1, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET version = excluded.version,"
            " applied_at = excluded.applied_at",
            (migration.version, datetime.now(UTC).isoformat()),
        )
    except Exception as exc:
        raise MigrationError(
            f"Learning Studio migration {migration.version} failed and was rolled back: {exc}"
        ) from exc
