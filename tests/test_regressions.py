"""Regressions for the defects found reviewing the first version of this PR.

Each test here reproduces something that was genuinely broken and verified
broken before it was fixed. They are gathered in one place because the value
of a regression test is knowing *why* it exists — scattered among the feature
tests, the reason for each of these would be invisible within a month.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from learning_studio import service, storage, tools
from learning_studio.config import ConfigError, LearningStudioConfig
from learning_studio.identity import Principal

LEARNER = Principal(
    profile="default", platform="telegram", user_id="9001", source="gateway_session"
)


# ── 1. Evidence overwrote explicit context ────────────────────────────────
#
# Explicit context and evidence shared one row per field, so the later write
# won regardless of authority. "The learner said X" was destroyed by "their
# answers suggest Y", and `superseded` came back empty because the losing
# value no longer existed to report.


def test_evidence_does_not_overwrite_explicit_context_in_one_save(hermes_home: Path):
    service.save_context(
        principal=LEARNER,
        temporary_context={"goal": "learner explicit"},
        evidence_context={"goal": "model inference"},
    )

    goal = service.get_context(principal=LEARNER)["resolved_context"]["goal"]

    assert goal["value"] == "learner explicit"
    assert goal["provenance"] == "explicit_request"
    assert [s["value"] for s in goal["superseded"]] == ["model inference"]


def test_evidence_saved_after_explicit_context_does_not_win(hermes_home: Path):
    service.save_context(principal=LEARNER, temporary_context={"goal": "explicit first"})
    service.save_context(principal=LEARNER, evidence_context={"goal": "evidence second"})

    goal = service.get_context(principal=LEARNER)["resolved_context"]["goal"]

    assert goal["value"] == "explicit first"


def test_explicit_context_saved_after_evidence_wins(hermes_home: Path):
    service.save_context(principal=LEARNER, evidence_context={"goal": "evidence first"})
    service.save_context(principal=LEARNER, temporary_context={"goal": "explicit second"})

    goal = service.get_context(principal=LEARNER)["resolved_context"]["goal"]

    assert goal["value"] == "explicit second"


def test_a_correction_beats_evidence(hermes_home: Path):
    service.save_context(principal=LEARNER, evidence_context={"goal": "inferred"})
    service.save_context(principal=LEARNER, corrections=[{"field": "goal", "value": "corrected"}])

    goal = service.get_context(principal=LEARNER)["resolved_context"]["goal"]

    assert goal["value"] == "corrected"
    assert goal["provenance"] == "explicit_correction"


def test_a_confirmed_track_beats_evidence(hermes_home: Path):
    service.save_context(
        principal=LEARNER,
        track={"name": "T", "confirmed": True, "context": {"goal": "confirmed"}},
    )
    service.save_context(principal=LEARNER, evidence_context={"goal": "inferred"})

    goal = service.get_context(principal=LEARNER)["resolved_context"]["goal"]

    assert goal["value"] == "confirmed"


def test_the_current_request_beats_every_stored_source(hermes_home: Path):
    service.save_context(
        principal=LEARNER,
        temporary_context={"goal": "stored explicit"},
        evidence_context={"goal": "stored evidence"},
        track={"name": "T", "confirmed": True, "context": {"goal": "stored track"}},
    )
    service.save_context(
        principal=LEARNER, corrections=[{"field": "goal", "value": "stored correction"}]
    )

    goal = service.get_context(principal=LEARNER, current_request={"goal": "asking right now"})[
        "resolved_context"
    ]["goal"]

    assert goal["value"] == "asking right now"
    assert len(goal["superseded"]) >= 3


def test_superseded_ordering_is_deterministic(hermes_home: Path):
    service.save_context(
        principal=LEARNER,
        temporary_context={"goal": "explicit"},
        evidence_context={"goal": "evidence"},
    )

    first = service.get_context(principal=LEARNER)["resolved_context"]["goal"]["superseded"]
    second = service.get_context(principal=LEARNER)["resolved_context"]["goal"]["superseded"]

    assert first == second


def test_competing_values_do_not_multiply_without_bound(hermes_home: Path):
    """One row per provenance, not one row per save."""
    for index in range(8):
        service.save_context(principal=LEARNER, evidence_context={"goal": f"guess {index}"})

    with storage.connect() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM context_values WHERE field = 'goal'"
        ).fetchone()["n"]

    assert rows == 1


# ── 2. Model-controlled identity ──────────────────────────────────────────


def test_a_learner_key_argument_no_longer_exists(hermes_home, gateway_session):
    result = json.loads(tools.handle_get_context({"learner_key": "9001"}))

    assert result["ok"] is False
    assert "learner_key" in result["error"]


def test_a_tool_argument_cannot_impersonate_another_principal(hermes_home, gateway_session):
    gateway_session(user_id="1001")
    tools.handle_save_context(
        {"track": {"name": "Victim track", "confirmed": True, "context": {"goal": "private"}}}
    )

    gateway_session(user_id="2002")
    seen = json.loads(tools.handle_get_context({}))

    assert seen["tracks"] == []


def test_the_same_user_id_on_another_platform_is_another_person(hermes_home, gateway_session):
    gateway_session(platform="telegram", user_id="1001")
    tools.handle_save_context({"temporary_context": {"goal": "telegram goal"}})

    gateway_session(platform="slack", user_id="1001")
    seen = json.loads(tools.handle_get_context({}))

    assert seen["temporary_context"] == {}


# ── 3. Accessibility was persisted without consent ────────────────────────


def test_accessibility_leaves_no_trace_in_the_database(hermes_home: Path):
    """Every table, not just the one that obviously holds it."""
    service.save_context(
        principal=LEARNER,
        temporary_context={"accessibility_needs": ["ADHD accommodations"]},
        evidence_context={"accessibility_needs": ["inferred need"]},
    )

    with storage.connect() as conn:
        for table, column in (
            ("context_values", "field"),
            ("context_revisions", "field"),
        ):
            count = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = 'accessibility_needs'"
            ).fetchone()["n"]
            assert count == 0, f"{table} holds accessibility data without consent"

        blob = " ".join(str(row[0]) for row in conn.execute("SELECT value FROM context_values"))
    assert "ADHD" not in blob
    assert "inferred need" not in blob


def test_consent_for_one_need_does_not_authorise_another(hermes_home: Path):
    result = service.save_context(
        principal=LEARNER,
        temporary_context={"accessibility_needs": ["a screen reader"]},
        accessibility_consent={
            "consent_statement": "remember I need captions",
            "needs": ["captions"],
        },
    )

    assert any(item["field"] == "accessibility_needs" for item in result["outcome"]["not_stored"])
    with storage.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM context_values WHERE field = 'accessibility_needs'"
        ).fetchone()["n"]
    assert count == 0


# ── 4. Inferred sensitive traits became durable candidates ────────────────


@pytest.mark.parametrize(
    "statement",
    ["The learner has ADHD", "The learner has dyslexia", "The learner has a disability"],
)
def test_repeated_evidence_cannot_produce_a_diagnosis_candidate(hermes_home: Path, statement):
    result = service.save_context(
        principal=LEARNER,
        memory_candidates=[
            {
                "category": "accessibility",
                "statement": statement,
                "evidence_summary": "Observed across several exercises.",
                "origin": "repeated_evidence",
                "confirmation_state": "learner_confirmed",
                "evidence_count": 9,
            }
        ],
        accessibility_consent={"consent_statement": "ok", "needs": ["x"]},
    )

    rejected = result["outcome"]["memory_candidates"]["rejected"]
    assert len(rejected) == 1
    assert "repeated_evidence" in rejected[0]["reason"]
    assert result["outcome"]["memory_candidates"]["accepted"] == []


def test_an_unconfirmed_sensitive_candidate_is_rejected(hermes_home: Path):
    result = service.save_context(
        principal=LEARNER,
        memory_candidates=[
            {
                "category": "accessibility",
                "statement": "Needs captions on audio",
                "evidence_summary": "Mentioned it once.",
                "origin": "explicit_durable_preference",
                "confirmation_state": "unconfirmed",
            }
        ],
        accessibility_consent={"consent_statement": "ok", "needs": ["captions"]},
    )

    assert "learner_confirmed" in result["outcome"]["memory_candidates"]["rejected"][0]["reason"]


def test_a_confirmed_consented_accessibility_candidate_is_accepted(hermes_home: Path):
    """The statement is the consented need itself — see Option A in the README."""
    result = service.save_context(
        principal=LEARNER,
        memory_candidates=[
            {
                "category": "accessibility",
                "statement": "captions on audio material",
                "evidence_summary": "Asked for this to be remembered.",
                "origin": "explicit_durable_preference",
                "confirmation_state": "learner_confirmed",
                "consented_need": "captions on audio material",
            }
        ],
        accessibility_consent={
            "consent_statement": "yes, remember I need captions",
            "needs": ["captions on audio material"],
        },
    )

    accepted = result["outcome"]["memory_candidates"]["accepted"]
    assert len(accepted) == 1
    assert accepted[0]["consent_reference"] == "yes, remember I need captions"
    assert accepted[0]["consented_need"] == "captions on audio material"


# ── 5. Migrations were not atomic ─────────────────────────────────────────
#
# `executescript()` commits before running, so DDL survived a rollback.
# Migrations are statement lists now; this drives the real production path.


def test_a_failing_migration_leaves_nothing_behind(hermes_home: Path, monkeypatch):
    monkeypatch.setattr(
        storage,
        "MIGRATIONS",
        [
            storage.Migration(
                version=1,
                statements=(
                    "CREATE TABLE canary (id TEXT PRIMARY KEY)",
                    "CREATE INDEX idx_canary ON canary (id)",
                    "CREATE TABLE broken (",
                ),
            )
        ],
    )

    with pytest.raises(storage.MigrationError):
        storage.initialize()

    with sqlite3.connect(storage.database_path()) as raw:
        objects = {row[0] for row in raw.execute("SELECT name FROM sqlite_master")}

    assert "canary" not in objects
    assert "idx_canary" not in objects
    assert "schema_version" not in objects


def test_nothing_in_the_package_calls_executescript():
    """Structural: a migration is data, so it cannot escape its transaction.

    Parsed rather than grepped, because the module docstring explains at
    length why ``executescript`` is not used and a substring search would
    match the explanation.
    """
    import ast

    package = Path(storage.__file__).parent
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "executescript"
            ):
                offenders.append(f"{path.name}:{node.lineno}")

    assert offenders == [], f"executescript commits before it runs: {offenders}"


def test_every_migration_is_a_statement_list():
    """A callable migration could reach for executescript again."""
    for migration in storage.MIGRATIONS:
        assert isinstance(migration.statements, tuple)
        assert all(isinstance(statement, str) for statement in migration.statements)


# ── 6. Concurrent initialization raced ────────────────────────────────────


def test_two_concurrent_first_initializations_both_succeed(hermes_home: Path):
    """Both callers saw version 0 before either took the write lock."""
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def initialise() -> None:
        try:
            barrier.wait(timeout=5)
            storage.initialize()
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=initialise) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == [], f"concurrent initialization failed: {errors}"
    with storage.connect() as conn:
        assert storage.read_schema_version(conn) == storage.SCHEMA_VERSION


# ── 7. Configuration and path resolution failed open ──────────────────────


def test_a_host_config_read_failure_fails_closed(monkeypatch):
    """Defaults would flip an operator's `false` to `true` silently."""
    import sys
    import types

    module = types.ModuleType("hermes_cli.config")
    module.load_config = lambda: (_ for _ in ()).throw(OSError("permission denied"))
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", module)

    from learning_studio.config import load_config

    with pytest.raises(ConfigError, match="could not be read"):
        load_config()


def test_a_host_yaml_parse_failure_fails_closed(monkeypatch):
    import sys
    import types

    module = types.ModuleType("hermes_cli.config")

    def explode():
        raise ValueError("while parsing a block mapping")

    module.load_config = explode
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", module)

    from learning_studio.config import load_config

    with pytest.raises(ConfigError):
        load_config()


def test_a_configured_accessibility_prohibition_survives_loading(monkeypatch):
    import sys
    import types

    module = types.ModuleType("hermes_cli.config")
    module.load_config = lambda: {"learning_studio": {"allow_durable_accessibility_needs": False}}
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", module)

    from learning_studio.config import load_config

    assert load_config().allow_durable_accessibility_needs is False


def test_a_host_profile_resolution_failure_fails_closed(monkeypatch):
    """Falling back to the env var here writes into the wrong profile."""
    import sys
    import types

    module = types.ModuleType("hermes_constants")

    def explode():
        raise RuntimeError("profile registry unavailable")

    module.get_hermes_home = explode
    monkeypatch.setitem(sys.modules, "hermes_constants", module)

    from learning_studio.paths import PathResolutionError, hermes_home

    with pytest.raises(PathResolutionError):
        hermes_home()


def test_a_storage_root_symlink_escape_is_rejected(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "somewhere-else"
    outside.mkdir()
    workspace = hermes_home / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "learning-studio").symlink_to(outside, target_is_directory=True)

    from learning_studio.paths import PathResolutionError, storage_root

    with pytest.raises(PathResolutionError, match="outside the active Hermes profile"):
        storage_root()


def test_a_symlink_escape_is_reported_to_the_agent_not_leaked(hermes_home, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    workspace = hermes_home / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "learning-studio").symlink_to(outside, target_is_directory=True)

    result = json.loads(tools.handle_get_context({}))

    assert result["ok"] is False
    assert str(outside) not in result["error"]


# ── 9. Objective updates crossed track boundaries ─────────────────────────


def _two_tracks(hermes_home: Path) -> tuple[str, str, str]:
    first = service.save_context(principal=LEARNER, track={"name": "A", "confirmed": True})
    track_a = first["outcome"]["track"]["track_id"]
    second = service.save_context(principal=LEARNER, track={"name": "B", "confirmed": True})
    track_b = second["outcome"]["track"]["track_id"]

    created = service.save_context(
        principal=LEARNER,
        objectives=[{"track_id": track_a, "behavior": "b", "condition": "c", "standard": "s"}],
    )
    return track_a, track_b, created["outcome"]["objectives"][0]["objective_id"]


def test_an_objective_cannot_be_updated_through_another_track(hermes_home: Path):
    track_a, track_b, objective_id = _two_tracks(hermes_home)

    with pytest.raises(service.NotFoundError):
        service.save_context(
            principal=LEARNER,
            objectives=[
                {"objective_id": objective_id, "track_id": track_b, "behavior": "hijacked"}
            ],
        )

    objectives = service.get_context(principal=LEARNER, track_id=track_a)["objectives"]
    assert objectives[0]["behavior"] == "b"


def test_an_omitted_status_does_not_reactivate_a_met_objective(hermes_home: Path):
    track_a, _, objective_id = _two_tracks(hermes_home)
    service.save_context(
        principal=LEARNER,
        objectives=[
            {
                "objective_id": objective_id,
                "track_id": track_a,
                "status": "met",
                "confirm_met": True,
            }
        ],
    )

    service.save_context(
        principal=LEARNER,
        objectives=[{"objective_id": objective_id, "track_id": track_a, "behavior": "reworded"}],
    )

    objectives = service.get_context(principal=LEARNER, track_id=track_a)["objectives"]
    assert objectives[0]["status"] == "met"
    assert objectives[0]["behavior"] == "reworded"


def test_an_omitted_status_does_not_reactivate_a_retired_objective(hermes_home: Path):
    track_a, _, objective_id = _two_tracks(hermes_home)
    service.save_context(
        principal=LEARNER,
        objectives=[{"objective_id": objective_id, "track_id": track_a, "status": "retired"}],
    )

    service.save_context(
        principal=LEARNER,
        objectives=[{"objective_id": objective_id, "track_id": track_a, "standard": "new"}],
    )

    objectives = service.get_context(principal=LEARNER, track_id=track_a)["objectives"]
    assert objectives[0]["status"] == "retired"


@pytest.mark.parametrize("closed", ["archived", "withdrawn"])
def test_a_closed_track_refuses_new_context_and_objectives(hermes_home: Path, closed: str):
    created = service.save_context(principal=LEARNER, track={"name": "T", "confirmed": True})
    track_id = created["outcome"]["track"]["track_id"]
    service.save_context(principal=LEARNER, track={"track_id": track_id, "status": closed})

    with pytest.raises(service.ValidationError, match=closed):
        service.save_context(
            principal=LEARNER,
            objectives=[{"track_id": track_id, "behavior": "b", "condition": "c", "standard": "s"}],
        )


def test_explicit_reactivation_then_update_succeeds(hermes_home: Path):
    created = service.save_context(principal=LEARNER, track={"name": "T", "confirmed": True})
    track_id = created["outcome"]["track"]["track_id"]
    service.save_context(principal=LEARNER, track={"track_id": track_id, "status": "archived"})

    service.save_context(
        principal=LEARNER,
        track={"track_id": track_id, "status": "active", "context": {"goal": "back at it"}},
    )

    result = service.get_context(principal=LEARNER, track_id=track_id)
    assert result["confirmed_context"]["goal"]["value"] == "back at it"


# ── 10. Rejected track context was silently discarded ─────────────────────


def test_context_from_a_rejected_track_is_kept_as_temporary(hermes_home: Path):
    """The response said it was kept; it was not."""
    result = service.save_context(
        principal=LEARNER,
        track={"name": "Unconfirmed", "context": {"goal": "keep me", "subject": "algebra"}},
    )

    assert result["outcome"]["track"]["status"] == "rejected"
    assert "kept as temporary" in result["outcome"]["track"]["reason"]

    stored = service.get_context(principal=LEARNER)
    assert stored["tracks"] == []
    assert stored["temporary_context"]["goal"]["value"] == "keep me"
    assert stored["temporary_context"]["subject"]["value"] == "algebra"


def test_a_rejected_track_still_drops_session_only_fields(hermes_home: Path):
    result = service.save_context(
        principal=LEARNER,
        track={
            "name": "Unconfirmed",
            "context": {"goal": "keep me", "accessibility_needs": ["drop me"]},
        },
    )

    assert any(item["field"] == "accessibility_needs" for item in result["outcome"]["not_stored"])
    stored = service.get_context(principal=LEARNER)["temporary_context"]
    assert "goal" in stored
    assert "accessibility_needs" not in stored


# ── 11. Candidate provenance was lost on write ────────────────────────────


def test_candidate_provenance_survives_a_database_round_trip(hermes_home: Path):
    service.save_context(
        principal=LEARNER,
        memory_candidates=[
            {
                "category": "durable_preference",
                "statement": "Prefers short sessions.",
                "evidence_summary": "Ended sessions early on several occasions.",
                "origin": "repeated_evidence",
                "evidence_count": 4,
                "confidence": "high",
            }
        ],
    )

    stored = service.get_context(principal=LEARNER, include_memory_candidates=True)
    candidate = stored["memory_candidates"][0]

    assert candidate["origin"] == "repeated_evidence"
    assert candidate["evidence_count"] == 4
    assert candidate["confidence"] == "high"
    assert candidate["confirmation_state"] == "unconfirmed"
    assert candidate["recommended_action"] == "add"
    assert candidate["created_at"] and candidate["updated_at"]


def test_a_replacement_target_survives_a_round_trip(hermes_home: Path):
    service.save_context(
        principal=LEARNER,
        memory_candidates=[
            {
                "category": "durable_preference",
                "statement": "Prefers detailed feedback.",
                "evidence_summary": "Corrected an earlier note.",
                "origin": "explicit_correction",
                "recommended_action": "replace",
                "replaces": "Prefers brief feedback.",
            }
        ],
    )

    candidate = service.get_context(principal=LEARNER, include_memory_candidates=True)[
        "memory_candidates"
    ][0]

    assert candidate["recommended_action"] == "replace"
    assert candidate["replaces"] == "Prefers brief feedback."


def test_no_raw_evidence_is_stored_with_a_candidate(hermes_home: Path):
    """Provenance, not transcripts."""
    with storage.connect() as conn:
        columns = (
            {row["name"] for row in conn.execute("PRAGMA table_info(memory_candidates)")}
            if storage.database_path().exists()
            else set()
        )
    service.save_context(
        principal=LEARNER,
        memory_candidates=[
            {
                "category": "durable_preference",
                "statement": "Prefers worked examples.",
                "evidence_summary": "Said so directly.",
                "origin": "explicit_durable_preference",
            }
        ],
    )
    with storage.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_candidates)")}

    for forbidden in ("raw_answer", "attempts", "transcript", "session_id"):
        assert forbidden not in columns


# ── 12. Expired data was never cleaned unless its owner returned ──────────


def test_expired_data_is_deleted_even_when_that_learner_never_returns(hermes_home: Path):
    absent = Principal(
        profile="default", platform="telegram", user_id="absent-1", source="gateway_session"
    )
    returning = Principal(
        profile="default", platform="telegram", user_id="returning-2", source="gateway_session"
    )

    service.save_context(principal=absent, temporary_context={"goal": "abandoned"})
    with storage.connect() as conn:
        conn.execute(
            "UPDATE learning_contexts SET expires_at = '2000-01-01T00:00:00+00:00'"
            " WHERE scope = 'temporary'"
        )
        conn.commit()

    # Only the *other* learner comes back.
    service.save_context(principal=returning, temporary_context={"goal": "active"})

    with storage.connect() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM context_values WHERE value LIKE '%abandoned%'"
        ).fetchone()["n"]
        contexts = conn.execute(
            "SELECT COUNT(*) AS n FROM learning_contexts"
            " WHERE expires_at IS NOT NULL AND expires_at <= '2001-01-01'"
        ).fetchone()["n"]

    assert remaining == 0, "the absent learner's expired context is still on disk"
    assert contexts == 0


def test_cleanup_never_touches_a_confirmed_track(hermes_home: Path):
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

    service.get_context(principal=LEARNER)
    result = service.get_context(principal=LEARNER)

    assert result["temporary_context"] == {}
    assert result["confirmed_context"]["goal"]["value"] == "survives"
    assert len(result["tracks"]) == 1


# ── 14. Ownership was not a database constraint ───────────────────────────


def test_the_database_rejects_a_track_claiming_the_wrong_profile(hermes_home: Path):
    service.save_context(principal=LEARNER, temporary_context={"goal": "x"})

    with storage.connect() as conn:
        learner_id = conn.execute("SELECT id FROM learners").fetchone()["id"]

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tracks"
                " (id, learner_id, profile_id, name, status, confirmed_at,"
                "  created_at, updated_at)"
                " VALUES ('t-bad', ?, 'another-profile', 'Sneaky', 'active',"
                " '2026-01-01', '2026-01-01', '2026-01-01')",
                (learner_id,),
            )


def test_the_database_rejects_a_context_value_claiming_the_wrong_learner(hermes_home: Path):
    service.save_context(principal=LEARNER, temporary_context={"goal": "x"})

    with storage.connect() as conn:
        context_id = conn.execute(
            "SELECT id FROM learning_contexts WHERE scope = 'temporary'"
        ).fetchone()["id"]

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO context_values"
                " (id, context_id, learner_id, profile_id, field, value, provenance,"
                "  confirmed, created_at, updated_at)"
                " VALUES ('cv-bad', ?, 'someone-else', 'default', 'goal', 'stolen',"
                " 'explicit_request', 1, '2026-01-01', '2026-01-01')",
                (context_id,),
            )


def test_the_database_rejects_an_objective_on_a_foreign_track(hermes_home: Path):
    created = service.save_context(principal=LEARNER, track={"name": "T", "confirmed": True})
    track_id = created["outcome"]["track"]["track_id"]

    with storage.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO objectives"
            " (id, track_id, learner_id, profile_id, behavior, condition, standard,"
            "  status, created_at, updated_at)"
            " VALUES ('o-bad', ?, 'someone-else', 'default', 'b', 'c', 's',"
            " 'active', '2026-01-01', '2026-01-01')",
            (track_id,),
        )


def test_deleting_a_learner_still_cascades(hermes_home: Path):
    """Composite keys must not have broken the cascade."""
    service.save_context(
        principal=LEARNER,
        track={"name": "T", "confirmed": True, "context": {"goal": "g"}},
    )

    with storage.connect() as conn, storage.transaction(conn):
        conn.execute("DELETE FROM learners")

    with storage.connect() as conn:
        for table in ("tracks", "learning_contexts", "context_values", "objectives"):
            count = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            assert count == 0, f"{table} survived the learner it belonged to"


# ── Config ceiling matches the advertised schema ──────────────────────────


def test_config_cannot_raise_a_limit_above_the_advertised_schema():
    """The schema string the model sees is the contract; config may only tighten."""
    from learning_studio.models import MAX_VALUE_CHARS

    with pytest.raises(ConfigError):
        LearningStudioConfig.from_mapping(
            {"learning_studio": {"max_context_value_chars": MAX_VALUE_CHARS + 1}}
        )


# ── Consent bound to the exact sensitive fact ─────────────────────────────
#
# Consent to remember captions authorised storing an ADHD diagnosis: the code
# checked that *some* consent existed, never that it covered the fact being
# proposed. Matching is now exact, on the canonical form of the need.

CAPTIONS_CONSENT = {
    "consent_statement": "Please remember that I need captions",
    "needs": ["captions on audio"],
}


def _candidate(**overrides):
    payload = {
        "category": "accessibility",
        "statement": "captions on audio",
        "evidence_summary": "The learner asked for this to be remembered",
        "origin": "explicit_durable_preference",
        "confirmation_state": "learner_confirmed",
        "consented_need": "captions on audio",
    }
    payload.update(overrides)
    return payload


def _save(consent=CAPTIONS_CONSENT, **overrides):
    return service.save_context(
        principal=LEARNER,
        accessibility_consent=consent,
        memory_candidates=[_candidate(**overrides)],
    )


def _accepted(result):
    return result["outcome"]["memory_candidates"]["accepted"]


def _rejection(result):
    rejected = result["outcome"]["memory_candidates"]["rejected"]
    assert len(rejected) == 1, result["outcome"]["memory_candidates"]
    return rejected[0]["reason"]


def test_captions_consent_does_not_authorise_an_adhd_candidate(hermes_home: Path):
    """The reported blocker, exactly as reported."""
    result = _save(
        statement="The learner has ADHD",
        evidence_summary="The learner explicitly stated this diagnosis",
        consented_need="ADHD",
    )

    assert _accepted(result) == []
    assert "does not match any need" in _rejection(result)


def test_captions_consent_does_not_authorise_a_screen_reader_candidate(hermes_home: Path):
    result = _save(statement="needs a screen reader", consented_need="needs a screen reader")

    assert _accepted(result) == []
    assert "does not match any need" in _rejection(result)


def test_case_and_whitespace_differences_still_match(hermes_home: Path):
    """Normalisation is deterministic, not fuzzy."""
    result = _save(statement="  Captions   On   Audio ", consented_need="CAPTIONS  on audio")

    accepted = _accepted(result)
    assert len(accepted) == 1
    assert accepted[0]["consented_need"] == "captions on audio"


def test_a_substring_is_not_consent(hermes_home: Path):
    """ "captions" consented does not cover "captions on all video and audio"."""
    result = service.save_context(
        principal=LEARNER,
        accessibility_consent={"consent_statement": "remember captions", "needs": ["captions"]},
        memory_candidates=[
            _candidate(
                statement="captions on all video and audio",
                consented_need="captions on all video and audio",
            )
        ],
    )

    assert _accepted(result) == []
    assert "does not match any need" in _rejection(result)


def test_an_unconfirmed_candidate_with_a_matching_need_is_rejected(hermes_home: Path):
    result = _save(confirmation_state="unconfirmed")

    assert _accepted(result) == []
    assert "learner_confirmed" in _rejection(result)


def test_repeated_evidence_with_a_matching_need_is_rejected(hermes_home: Path):
    result = _save(origin="repeated_evidence", evidence_count=9)

    assert _accepted(result) == []
    assert "repeated_evidence" in _rejection(result)


def test_an_exact_confirmed_consented_candidate_is_accepted(hermes_home: Path):
    result = _save()

    accepted = _accepted(result)
    assert len(accepted) == 1
    assert accepted[0]["consented_need"] == "captions on audio"
    assert accepted[0]["consent_reference"] == CAPTIONS_CONSENT["consent_statement"]


def test_sensitive_content_under_a_non_accessibility_category_is_rejected(hermes_home: Path):
    result = _save(
        category="durable_preference",
        statement="Has ADHD so prefers short sessions",
        consented_need=None,
    )

    assert _accepted(result) == []
    assert "sensitive health, disability" in _rejection(result)


def test_an_accessibility_candidate_without_a_consented_need_is_rejected(hermes_home: Path):
    result = _save(consented_need=None)

    assert _accepted(result) == []
    assert "requires 'consented_need'" in _rejection(result)


def test_a_consented_need_without_any_consent_is_rejected(hermes_home: Path):
    result = service.save_context(principal=LEARNER, memory_candidates=[_candidate()])

    assert _accepted(result) == []
    assert "requires accessibility_consent" in _rejection(result)


def test_consented_need_is_refused_on_a_non_sensitive_category(hermes_home: Path):
    result = _save(
        category="durable_preference",
        statement="Prefers worked examples",
        evidence_summary="Said so directly",
        consented_need="captions on audio",
    )

    assert _accepted(result) == []
    assert "applies only to an accessibility candidate" in _rejection(result)


def test_an_unknown_consent_property_is_refused_by_runtime_validation(hermes_home, gateway_session):
    result = json.loads(
        tools.handle_save_context(
            {
                "accessibility_consent": {
                    "consent_statement": "s",
                    "needs": ["n"],
                    "scope": "everything",
                }
            }
        )
    )

    assert result["ok"] is False
    assert "scope" in result["error"]


def test_an_unknown_candidate_consent_property_is_refused(hermes_home, gateway_session):
    result = json.loads(
        tools.handle_save_context(
            {"memory_candidates": [dict(_candidate(), consent_override=True)]}
        )
    )

    assert result["ok"] is False
    assert "consent_override" in result["error"]


def test_an_accepted_candidate_round_trips_its_consent_binding(hermes_home: Path):
    _save()

    stored = service.get_context(principal=LEARNER, include_memory_candidates=True)
    candidate = stored["memory_candidates"][0]

    assert candidate["origin"] == "explicit_durable_preference"
    assert candidate["confirmation_state"] == "learner_confirmed"
    assert candidate["consent_reference"] == CAPTIONS_CONSENT["consent_statement"]
    assert candidate["consented_need"] == "captions on audio"
    assert candidate["created_at"] and candidate["updated_at"]


def test_a_rejected_mismatch_persists_nothing(hermes_home: Path):
    _save(statement="The learner has ADHD", consented_need="ADHD")

    with storage.connect() as conn:
        candidates = conn.execute("SELECT COUNT(*) AS n FROM memory_candidates").fetchone()["n"]
        values = conn.execute(
            "SELECT COUNT(*) AS n FROM context_values WHERE field = 'accessibility_needs'"
        ).fetchone()["n"]
        blob = " ".join(
            str(row[0]) for row in conn.execute("SELECT statement FROM memory_candidates")
        )

    assert candidates == 0
    assert values == 0
    assert "ADHD" not in blob


def test_each_consented_need_authorises_only_itself(hermes_home: Path):
    """Listing two needs does not make either cover the other, or a third."""
    consent = {
        "consent_statement": "remember both of these",
        "needs": ["captions on audio", "extra time on written tasks"],
    }

    result = service.save_context(
        principal=LEARNER,
        accessibility_consent=consent,
        memory_candidates=[
            _candidate(statement="captions on audio", consented_need="captions on audio"),
            _candidate(
                statement="extra time on written tasks",
                consented_need="extra time on written tasks",
            ),
            _candidate(statement="a quiet room", consented_need="a quiet room"),
        ],
    )

    accepted = sorted(item["consented_need"] for item in _accepted(result))
    assert accepted == ["captions on audio", "extra time on written tasks"]
    assert len(result["outcome"]["memory_candidates"]["rejected"]) == 1


def test_the_statement_must_be_the_consented_need(hermes_home: Path):
    """Presentation prose is never the authorisation."""
    result = _save(statement="I will remember that you need captions on audio")

    assert _accepted(result) == []
    assert "must be exactly the consented need" in _rejection(result)


def test_need_normalisation_is_deterministic_and_not_fuzzy():
    from learning_studio.models import normalize_need

    assert normalize_need("  Captions   On  Audio ") == "captions on audio"
    assert normalize_need("CAPTIONS ON AUDIO") == normalize_need("captions on audio")
    # NFKC folds compatibility forms; it does not fold different needs together.
    assert normalize_need("ﬁne print") == "fine print"
    assert normalize_need("captions") != normalize_need("captions on audio")


# ── Identity isolation between tests ──────────────────────────────────────
#
# Running the suite from inside a live Telegram-hosted Hermes session put
# HERMES_SESSION_PLATFORM=telegram in the environment, so a test expecting the
# local-CLI principal silently asserted against a real account instead.


def test_a_local_test_does_not_inherit_a_live_gateway_identity(hermes_home: Path):
    """The autouse `isolated_identity` fixture must have cleared this."""
    import os

    from learning_studio.identity import resolve_principal

    assert os.environ.get("HERMES_SESSION_PLATFORM") is None
    assert os.environ.get("HERMES_SESSION_USER_ID") is None
    assert resolve_principal().platform == "local"


def test_inherited_session_variables_are_cleared_before_each_test(monkeypatch):
    """Simulates pytest being started from a live Telegram session."""
    import os
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        import os, sys
        sys.path.insert(0, %r)
        from learning_studio.identity import resolve_principal
        print(resolve_principal().platform)
        """
    ) % str(Path(__file__).resolve().parent.parent)

    env = dict(os.environ)
    env["HERMES_SESSION_PLATFORM"] = "telegram"
    env["HERMES_SESSION_USER_ID"] = "750733916"

    # Without isolation the resolver sees the inherited session...
    leaked = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert leaked == "telegram", "the reproduction itself no longer reproduces"

    # ...and inside the suite, the fixture has already removed it.
    from learning_studio.identity import resolve_principal

    assert resolve_principal().platform == "local"


def test_the_fixture_restores_variables_a_test_set_itself(gateway_session):
    """`gateway_session` binds an identity; the next test must not see it."""
    import os

    assert os.environ["HERMES_SESSION_USER_ID"] == "1001"


def test_the_previous_tests_identity_did_not_survive():
    import os

    from learning_studio.identity import resolve_principal

    assert os.environ.get("HERMES_SESSION_USER_ID") is None
    assert resolve_principal().platform == "local"


def test_identity_isolation_survives_a_failing_test():
    """State is cleaned in a finally, so a raising test cannot leak it."""
    import os

    os.environ["HERMES_SESSION_PLATFORM"] = "telegram"
    os.environ["HERMES_SESSION_USER_ID"] = "leaky-999"
    # The fixture's finally block restores this regardless of how the test ends.


def test_the_failing_tests_identity_did_not_leak():
    import os

    from learning_studio.identity import resolve_principal

    assert os.environ.get("HERMES_SESSION_USER_ID") is None
    assert resolve_principal().platform == "local"


def test_a_first_time_learner_still_gets_the_requested_keys(hermes_home, gateway_session):
    """Response shape must not depend on whether the learner exists yet."""
    result = json.loads(tools.handle_get_context({"include_memory_candidates": True}))

    assert result["ok"] is True
    assert result["memory_candidates"] == []
