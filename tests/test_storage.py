"""Storage: profile-safe paths, deterministic migrations, connection pragmas.

Every test here runs against a temporary ``HERMES_HOME``. A test that touched
the developer's real profile would be both a privacy leak and a source of
cross-test coupling, so ``hermes_home`` (see conftest) is mandatory rather
than optional.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from learning_studio import storage
from learning_studio.config import LearningStudioConfig
from learning_studio.paths import storage_root

# ── Where the database lives ──────────────────────────────────────────────


def test_storage_root_is_under_the_hermes_home_workspace(hermes_home: Path):
    root = storage_root()

    assert root == hermes_home / "workspace" / "learning-studio"


def test_storage_root_follows_hermes_home_rather_than_the_cwd(
    hermes_home: Path, monkeypatch, tmp_path: Path
):
    """The process CWD must never be the persistence root."""
    elsewhere = tmp_path / "some-unrelated-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert storage_root() == hermes_home / "workspace" / "learning-studio"


def test_database_path_is_inside_the_storage_root(hermes_home: Path):
    assert storage.database_path().parent == storage_root()


def test_initialization_creates_the_directory_with_restrictive_permissions(hermes_home: Path):
    storage.initialize()

    root = storage_root()
    assert root.is_dir()
    assert root.stat().st_mode & 0o077 == 0, "storage root must not be group/world accessible"


def test_initialization_writes_nothing_outside_the_hermes_home(hermes_home: Path, tmp_path: Path):
    """Catches a hard-coded ~/.hermes or a stray relative path."""
    before = {p for p in tmp_path.rglob("*") if hermes_home not in p.parents and p != hermes_home}

    storage.initialize()
    with storage.connect() as conn:
        conn.execute("SELECT 1")

    after = {p for p in tmp_path.rglob("*") if hermes_home not in p.parents and p != hermes_home}
    assert after == before


# ── Migrations ────────────────────────────────────────────────────────────


def test_initialize_from_an_empty_database_reaches_the_current_version(hermes_home: Path):
    storage.initialize()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == storage.SCHEMA_VERSION


def test_initialize_is_idempotent(hermes_home: Path):
    storage.initialize()
    storage.initialize()
    storage.initialize()

    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == storage.SCHEMA_VERSION
        rows = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        assert rows == 1, "schema_version must hold exactly one row"


def test_migrations_are_applied_in_deterministic_order():
    versions = [migration.version for migration in storage.MIGRATIONS]

    assert versions == sorted(versions), "migrations must be ordered by version"
    assert versions == list(range(1, len(versions) + 1)), "versions must be contiguous from 1"
    assert versions[-1] == storage.SCHEMA_VERSION


def test_a_failing_migration_rolls_back_completely(hermes_home: Path, monkeypatch):
    """A half-applied schema is worse than no schema at all.

    This drives the *production* execution path: a real ``Migration`` of real
    statements, applied by ``initialize()``. An earlier version of this test
    ran the DDL through a callback that used ``executescript``, which commits
    before it runs — so the canary survived the rollback and the test still
    passed. Migrations are statement lists now precisely so a migration
    cannot escape its transaction, and this proves it.
    """
    monkeypatch.setattr(
        storage,
        "MIGRATIONS",
        [
            storage.Migration(
                version=1,
                statements=(
                    "CREATE TABLE canary (id TEXT PRIMARY KEY)",
                    "CREATE INDEX idx_canary ON canary (id)",
                    "THIS IS NOT VALID SQL",
                ),
            )
        ],
    )

    with pytest.raises(storage.MigrationError):
        storage.initialize()

    with sqlite3.connect(storage.database_path()) as raw:
        listing = raw.execute("SELECT name FROM sqlite_master")
        objects = {row[0] for row in listing}
    assert "canary" not in objects, "failed migration left its table behind"
    assert "idx_canary" not in objects, "failed migration left its index behind"
    assert "schema_version" not in objects, "the version advanced despite the failure"


def test_a_database_newer_than_the_code_fails_loudly(hermes_home: Path):
    """Never silently delete, reset, or downgrade an unfamiliar database."""
    storage.initialize()
    with storage.connect() as conn:
        conn.execute("UPDATE schema_version SET version = ?", (storage.SCHEMA_VERSION + 5,))
        conn.commit()

    with pytest.raises(storage.IncompatibleSchemaError):
        storage.initialize()


def test_an_incompatible_database_is_not_recreated(hermes_home: Path):
    storage.initialize()
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO learners"
            " (id, profile_id, principal_digest, platform, created_at, updated_at)"
            " VALUES ('l1', 'default', 'digest', 'telegram', '2026-01-01T00:00:00Z',"
            " '2026-01-01T00:00:00Z')"
        )
        conn.execute("UPDATE schema_version SET version = ?", (storage.SCHEMA_VERSION + 5,))
        conn.commit()

    with pytest.raises(storage.IncompatibleSchemaError):
        storage.initialize()

    with sqlite3.connect(storage.database_path()) as raw:
        surviving = raw.execute("SELECT COUNT(*) FROM learners").fetchone()[0]
    assert surviving == 1, "the existing data must survive an incompatible-version refusal"


# ── Connection configuration ──────────────────────────────────────────────


def test_foreign_keys_are_enforced_on_every_connection(hermes_home: Path):
    storage.initialize()

    with storage.connect() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tracks (id, learner_id, profile_id, name, status,"
                " confirmed_at, created_at, updated_at)"
                " VALUES ('t1', 'no-such-learner', 'default', 'Track', 'active',"
                " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )


def test_busy_timeout_is_bounded_and_applied(hermes_home: Path):
    storage.initialize()
    config = LearningStudioConfig.from_mapping({"learning_studio": {"busy_timeout_ms": 1234}})

    with storage.connect(config=config) as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1234


def test_wal_is_the_default_journal_mode(hermes_home: Path):
    storage.initialize()

    with storage.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_journal_mode_falls_back_when_wal_is_unavailable(hermes_home: Path):
    """A filesystem that cannot do WAL must degrade, not crash."""
    storage.initialize()
    config = LearningStudioConfig.from_mapping({"learning_studio": {"journal_mode": "delete"}})

    with storage.connect(config=config) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"


def test_connect_returns_a_new_connection_each_time(hermes_home: Path):
    """A module-level shared connection is not safe across threads."""
    storage.initialize()

    with storage.connect() as first, storage.connect() as second:
        assert first is not second


def test_two_hermes_homes_get_independent_databases(tmp_path: Path, monkeypatch):
    first = tmp_path / "home-a"
    second = tmp_path / "home-b"

    monkeypatch.setenv("HERMES_HOME", str(first))
    storage.initialize()
    path_a = storage.database_path()

    monkeypatch.setenv("HERMES_HOME", str(second))
    storage.initialize()
    path_b = storage.database_path()

    assert path_a != path_b
    assert path_a.is_file() and path_b.is_file()


# ── Untrusted paths ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "parts",
    [
        ("..", "escaped.db"),
        ("..", "..", "etc", "passwd"),
        ("nested", "..", "..", "outside"),
        ("/etc/passwd",),
        ("/tmp/absolute",),
    ],
)
def test_a_path_that_escapes_the_storage_root_is_refused(hermes_home: Path, parts):
    """Names reaching the storage layer originate in tool arguments."""
    from learning_studio.paths import resolve_within_storage

    with pytest.raises(ValueError, match="escapes"):
        resolve_within_storage(*parts)


@pytest.mark.parametrize("parts", [("sub", "file.db"), ("file.db",), ("a", "b", "c.json")])
def test_a_path_inside_the_storage_root_is_allowed(hermes_home: Path, parts):
    from learning_studio.paths import resolve_within_storage

    resolved = resolve_within_storage(*parts)

    assert storage_root() in resolved.parents or resolved.parent == storage_root()
