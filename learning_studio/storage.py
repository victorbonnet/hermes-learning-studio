"""SQLite persistence: connections, pragmas, and versioned migrations.

Design constraints that shaped this module:

- **No shared connection object.** ``sqlite3`` connections are not safe to
  hand between threads, and Hermes runs tool handlers from more than one.
  Every caller opens its own via :func:`connect`.
- **Migrations are all-or-nothing.** A partially applied schema is harder to
  recover from than a failed startup, so each migration runs inside its own
  transaction and rolls back completely on failure.
- **An unknown database is never repaired.** If the file was written by a
  newer version of this plugin, the only safe action is to refuse. Deleting
  or "resetting" it would destroy a learner's record to make the code happy.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
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
    version: int
    apply: Callable[[sqlite3.Connection], None]


def database_path() -> Path:
    return storage_root() / DATABASE_FILENAME


# ── Schema ────────────────────────────────────────────────────────────────


def _migration_001(conn: sqlite3.Connection) -> None:
    """Initial schema.

    Two conventions run through every table:

    - ``profile_id`` and ``learner_id`` are denormalised onto every
      learner-owned row so authorisation is a ``WHERE`` clause on the row
      itself, not a join the caller might forget. Foreign keys enforce
      referential integrity; they do not enforce *who may read a row*.
    - Identifiers are opaque generated tokens. Nothing keys on a username, a
      display name, or any other label a learner can change.
    """
    conn.executescript(
        """
        CREATE TABLE schema_version (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            version     INTEGER NOT NULL,
            applied_at  TEXT    NOT NULL
        );

        -- Internal key/value store. Currently holds only the per-database
        -- salt for learner digests; never exposed through a tool.
        CREATE TABLE studio_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- A learner is a person within one profile. One profile may serve
        -- several people (a family device, a shared assistant), so the
        -- profile alone is never the identity.
        CREATE TABLE learners (
            id              TEXT PRIMARY KEY,
            profile_id      TEXT NOT NULL,
            -- Salted digest of the caller's learner key, never the key
            -- itself: the plugin can match a returning learner without
            -- holding their platform identifier on disk.
            learner_digest  TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            UNIQUE (profile_id, learner_digest)
        );

        -- Sustained work toward a goal. Only ever created with explicit
        -- learner confirmation; see service.save_context.
        CREATE TABLE tracks (
            id           TEXT PRIMARY KEY,
            learner_id   TEXT NOT NULL REFERENCES learners(id) ON DELETE CASCADE,
            profile_id   TEXT NOT NULL,
            name         TEXT NOT NULL,
            status       TEXT NOT NULL
                         CHECK (status IN ('active', 'archived', 'withdrawn')),
            confirmed_at TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            UNIQUE (learner_id, name)
        );
        CREATE INDEX idx_tracks_owner ON tracks (profile_id, learner_id, status);

        -- A container for context values: either the learner's single
        -- temporary context, or the durable context of one track.
        CREATE TABLE learning_contexts (
            id          TEXT PRIMARY KEY,
            learner_id  TEXT NOT NULL REFERENCES learners(id) ON DELETE CASCADE,
            profile_id  TEXT NOT NULL,
            scope       TEXT NOT NULL CHECK (scope IN ('temporary', 'track')),
            track_id    TEXT REFERENCES tracks(id) ON DELETE CASCADE,
            expires_at  TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            -- A temporary context belongs to nothing; a track context must
            -- name its track. This is what keeps the two kinds from being
            -- confused for one another at the storage layer.
            CHECK (
                (scope = 'track'     AND track_id IS NOT NULL AND expires_at IS NULL)
                OR
                (scope = 'temporary' AND track_id IS NULL)
            )
        );
        CREATE UNIQUE INDEX idx_one_temporary_context_per_learner
            ON learning_contexts (learner_id) WHERE scope = 'temporary';
        CREATE UNIQUE INDEX idx_one_context_per_track
            ON learning_contexts (track_id) WHERE scope = 'track';

        CREATE TABLE context_values (
            id          TEXT PRIMARY KEY,
            context_id  TEXT NOT NULL REFERENCES learning_contexts(id) ON DELETE CASCADE,
            learner_id  TEXT NOT NULL REFERENCES learners(id) ON DELETE CASCADE,
            profile_id  TEXT NOT NULL,
            field       TEXT NOT NULL,
            value       TEXT NOT NULL,
            provenance  TEXT NOT NULL,
            confirmed   INTEGER NOT NULL CHECK (confirmed IN (0, 1)),
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            UNIQUE (context_id, field)
        );
        CREATE INDEX idx_context_values_owner ON context_values (profile_id, learner_id);

        -- What changed, when, why, and what it replaced. Deliberately not a
        -- transcript: the reason is a category, and the values are the
        -- structured before/after, never the conversation that produced them.
        CREATE TABLE context_revisions (
            id                  TEXT PRIMARY KEY,
            context_id          TEXT NOT NULL REFERENCES learning_contexts(id) ON DELETE CASCADE,
            learner_id          TEXT NOT NULL REFERENCES learners(id) ON DELETE CASCADE,
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
            created_at          TEXT NOT NULL
        );
        CREATE INDEX idx_revisions_owner
            ON context_revisions (profile_id, learner_id, context_id, created_at);

        CREATE TABLE objectives (
            id          TEXT PRIMARY KEY,
            track_id    TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
            learner_id  TEXT NOT NULL REFERENCES learners(id) ON DELETE CASCADE,
            profile_id  TEXT NOT NULL,
            behavior    TEXT NOT NULL,
            condition   TEXT NOT NULL,
            standard    TEXT NOT NULL,
            status      TEXT NOT NULL
                        CHECK (status IN ('proposed', 'active', 'met', 'retired')),
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX idx_objectives_owner ON objectives (profile_id, learner_id, track_id);

        -- Proposals for the agent to weigh. Not memory writes: this plugin
        -- has no path to Hermes memory and never will have one.
        CREATE TABLE memory_candidates (
            id                 TEXT PRIMARY KEY,
            learner_id         TEXT NOT NULL REFERENCES learners(id) ON DELETE CASCADE,
            profile_id         TEXT NOT NULL,
            track_id           TEXT REFERENCES tracks(id) ON DELETE SET NULL,
            category           TEXT NOT NULL,
            statement          TEXT NOT NULL,
            evidence_summary   TEXT NOT NULL,
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
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL
        );
        CREATE INDEX idx_candidates_owner ON memory_candidates (profile_id, learner_id);
        """
    )


#: Ordered, contiguous from 1. The list order is the application order.
MIGRATIONS: list[Migration] = [
    Migration(version=1, apply=_migration_001),
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
    """Create or migrate the database. Safe to call on every registration.

    Raises :class:`IncompatibleSchemaError` if the database is newer than
    this code, and :class:`MigrationError` if a migration fails — in which
    case nothing from that migration has been applied.
    """
    config = config or LearningStudioConfig()
    with connect(config) as conn:
        current = read_schema_version(conn)

        if current > SCHEMA_VERSION:
            raise IncompatibleSchemaError(
                f"The Learning Studio database is at schema version {current}, but this "
                f"version of the plugin understands {SCHEMA_VERSION}. Upgrade the plugin. "
                "The database has been left untouched."
            )

        for migration in MIGRATIONS:
            if migration.version <= current:
                continue
            _apply(conn, migration)


def _apply(conn: sqlite3.Connection, migration: Migration) -> None:
    from datetime import datetime

    try:
        with transaction(conn):
            migration.apply(conn)
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
