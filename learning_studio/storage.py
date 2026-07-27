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


#: Ordered, contiguous from 1. The list order is the application order.
MIGRATIONS: list[Migration] = [
    Migration(version=1, statements=_MIGRATION_001),
    Migration(version=2, statements=_MIGRATION_002),
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


def initialize(config: LearningStudioConfig | None = None) -> None:
    """Create or migrate the database. Safe to call concurrently and repeatedly.

    The version is read twice on purpose. The first read is an unlocked fast
    path for the overwhelmingly common case of an up-to-date database. The
    second happens *after* ``BEGIN IMMEDIATE`` has taken the write lock,
    because between the two another process may have done the work — and
    acting on the stale answer would mean trying to create tables that now
    exist.

    Raises :class:`IncompatibleSchemaError` if the database is newer than
    this code, and :class:`MigrationError` if a migration fails, in which
    case nothing from the upgrade has been applied.
    """
    config = config or LearningStudioConfig()
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
