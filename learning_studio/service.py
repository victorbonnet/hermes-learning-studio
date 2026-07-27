"""The Learning Studio service: reads, writes, and the rules that gate them.

This is where the plugin's promises are actually kept, so the rules are worth
stating before the code:

**Isolation is enforced here, not in the tool handler.** Every learner-owned
query carries ``profile_id`` and ``learner_id`` in its ``WHERE`` clause. A
handler that forgot to check ownership would still not be able to read
another learner's track, because there is no query that can.

**Nothing becomes durable by accident.** A track is created only when the
caller passes an explicit confirmation flag. Repetition, agent confidence,
and prior sessions are not confirmation, and there is no code path that
treats them as such.

**Not-found and not-yours are the same answer.** Distinguishing them would
turn a track ID into an oracle for whether another learner exists.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from . import candidates as candidate_rules
from . import storage
from .config import LearningStudioConfig, load_config
from .context import Candidate, candidates_from_config, candidates_from_request, resolve
from .models import (
    CONTEXT_FIELDS,
    DURABLE_WRITE_PROVENANCES,
    SESSION_ONLY_FIELDS,
    ChangeReason,
    ContextScope,
    ObjectiveStatus,
    Provenance,
    TrackStatus,
    decode_value,
    encode_value,
    validate_context_payload,
    validate_field_value,
    validate_learner_key,
    validate_track_name,
)
from .paths import profile_id

#: Returned whenever a caller names an object they do not own, or one that
#: does not exist. The two cases are deliberately indistinguishable.
NOT_FOUND_MESSAGE = "No such track for this learner."


class ServiceError(Exception):
    """A request was refused. The message is safe to show the agent."""


class ValidationError(ServiceError):
    """Input failed validation."""


class NotFoundError(ServiceError):
    """The object does not exist, or is not this learner's. Never say which."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


# ── Learner identity ──────────────────────────────────────────────────────


def _learner_salt(conn: sqlite3.Connection) -> str:
    """Return this database's digest salt, creating it on first use.

    Salting means the stored digests cannot be checked against a precomputed
    table of platform user IDs. Someone who obtains the database file learns
    that *some* learners exist, not who they are.
    """
    row = conn.execute("SELECT value FROM studio_meta WHERE key = 'learner_salt'").fetchone()
    if row:
        return str(row["value"])
    salt = secrets.token_hex(32)
    conn.execute(
        "INSERT INTO studio_meta (key, value) VALUES ('learner_salt', ?)"
        " ON CONFLICT(key) DO NOTHING",
        (salt,),
    )
    row = conn.execute("SELECT value FROM studio_meta WHERE key = 'learner_salt'").fetchone()
    return str(row["value"])


def _digest(conn: sqlite3.Connection, profile: str, learner_key: str) -> str:
    """Derive the lookup digest for a learner.

    The caller's key — a platform user ID, typically — is never written to
    disk. Only this digest is, so the database holds no directly usable
    identifier for the person it describes.
    """
    salt = _learner_salt(conn)
    return hmac.new(
        salt.encode("utf-8"), f"{profile}\x00{learner_key}".encode(), hashlib.sha256
    ).hexdigest()


def _get_or_create_learner(conn: sqlite3.Connection, profile: str, learner_key: str) -> str:
    digest = _digest(conn, profile, learner_key)
    row = conn.execute(
        "SELECT id FROM learners WHERE profile_id = ? AND learner_digest = ?",
        (profile, digest),
    ).fetchone()
    if row:
        return str(row["id"])

    learner_id = _new_id()
    now = _now()
    conn.execute(
        "INSERT INTO learners (id, profile_id, learner_digest, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (learner_id, profile, digest, now, now),
    )
    return learner_id


def _find_learner(conn: sqlite3.Connection, profile: str, learner_key: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM learners WHERE profile_id = ? AND learner_digest = ?",
        (profile, _digest(conn, profile, learner_key)),
    ).fetchone()
    return str(row["id"]) if row else None


# ── Ownership-checked lookups ─────────────────────────────────────────────


def _owned_track(
    conn: sqlite3.Connection, profile: str, learner_id: str, track_id: str
) -> sqlite3.Row:
    """Fetch a track, or raise. Ownership is part of the query, not a later check."""
    row = conn.execute(
        "SELECT * FROM tracks WHERE id = ? AND profile_id = ? AND learner_id = ?",
        (track_id, profile, learner_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    return row


def _track_by_name(
    conn: sqlite3.Connection, profile: str, learner_id: str, name: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM tracks WHERE profile_id = ? AND learner_id = ? AND name = ?",
        (profile, learner_id, name),
    ).fetchone()


def _list_tracks(conn: sqlite3.Connection, profile: str, learner_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM tracks WHERE profile_id = ? AND learner_id = ? ORDER BY created_at, id",
            (profile, learner_id),
        )
    )


# ── Contexts ──────────────────────────────────────────────────────────────


def _temporary_context(
    conn: sqlite3.Connection, profile: str, learner_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM learning_contexts"
        " WHERE profile_id = ? AND learner_id = ? AND scope = 'temporary'",
        (profile, learner_id),
    ).fetchone()


def _track_context(
    conn: sqlite3.Connection, profile: str, learner_id: str, track_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM learning_contexts"
        " WHERE profile_id = ? AND learner_id = ? AND scope = 'track' AND track_id = ?",
        (profile, learner_id, track_id),
    ).fetchone()


def _ensure_context(
    conn: sqlite3.Connection,
    profile: str,
    learner_id: str,
    scope: ContextScope,
    track_id: str | None,
    config: LearningStudioConfig,
) -> str:
    existing = (
        _temporary_context(conn, profile, learner_id)
        if scope is ContextScope.TEMPORARY
        else _track_context(conn, profile, learner_id, str(track_id))
    )
    now = _now()
    if existing:
        context_id = str(existing["id"])
        # Touching a temporary context restarts its retention window: it
        # tracks the conversation, so activity is what keeps it alive.
        expires = _expiry(config) if scope is ContextScope.TEMPORARY else None
        conn.execute(
            "UPDATE learning_contexts SET updated_at = ?, expires_at = ?"
            " WHERE id = ? AND profile_id = ? AND learner_id = ?",
            (now, expires, context_id, profile, learner_id),
        )
        return context_id

    context_id = _new_id()
    conn.execute(
        "INSERT INTO learning_contexts"
        " (id, learner_id, profile_id, scope, track_id, expires_at, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            context_id,
            learner_id,
            profile,
            scope.value,
            track_id,
            _expiry(config) if scope is ContextScope.TEMPORARY else None,
            now,
            now,
        ),
    )
    return context_id


def _expiry(config: LearningStudioConfig) -> str:
    return (datetime.now(UTC) + timedelta(hours=config.temporary_context_ttl_hours)).isoformat()


def _is_expired(row: sqlite3.Row | None) -> bool:
    if row is None:
        return False
    expires_at = row["expires_at"]
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(str(expires_at)) <= datetime.now(UTC)
    except ValueError:  # pragma: no cover - only reachable via manual DB edits
        return False


def purge_expired(conn: sqlite3.Connection, profile: str, learner_id: str) -> int:
    """Delete temporary contexts past their retention window.

    Cascades to their values and revisions. Confirmed tracks are untouched —
    only unconfirmed conversational evidence expires.
    """
    cursor = conn.execute(
        "DELETE FROM learning_contexts"
        " WHERE profile_id = ? AND learner_id = ? AND scope = 'temporary'"
        "   AND expires_at IS NOT NULL AND expires_at <= ?",
        (profile, learner_id, _now()),
    )
    return cursor.rowcount or 0


def _context_values(
    conn: sqlite3.Connection, profile: str, learner_id: str, context_id: str
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM context_values"
            " WHERE profile_id = ? AND learner_id = ? AND context_id = ?"
            " ORDER BY field",
            (profile, learner_id, context_id),
        )
    )


def _write_value(
    conn: sqlite3.Connection,
    *,
    profile: str,
    learner_id: str,
    context_id: str,
    field: str,
    value: Any,
    provenance: Provenance,
    change_reason: ChangeReason,
) -> dict[str, Any]:
    """Upsert one context value and record a revision if it actually changed.

    Revisions capture the structured before/after and *why*, never the
    conversation that produced it — a transcript in a revision log would be
    both a privacy problem and useless for resolving conflicts.
    """
    encoded = encode_value(value)
    now = _now()
    existing = conn.execute(
        "SELECT * FROM context_values"
        " WHERE context_id = ? AND field = ? AND profile_id = ? AND learner_id = ?",
        (context_id, field, profile, learner_id),
    ).fetchone()

    confirmed = 1 if provenance in DURABLE_WRITE_PROVENANCES else 0

    if existing is None:
        conn.execute(
            "INSERT INTO context_values"
            " (id, context_id, learner_id, profile_id, field, value, provenance,"
            "  confirmed, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _new_id(),
                context_id,
                learner_id,
                profile,
                field,
                encoded,
                provenance.value,
                confirmed,
                now,
                now,
            ),
        )
        _record_revision(
            conn,
            profile=profile,
            learner_id=learner_id,
            context_id=context_id,
            field=field,
            previous_value=None,
            previous_provenance=None,
            new_value=encoded,
            provenance=provenance,
            change_reason=change_reason,
        )
        return {"field": field, "change": "created"}

    if existing["value"] == encoded and existing["provenance"] == provenance.value:
        return {"field": field, "change": "unchanged"}

    conn.execute(
        "UPDATE context_values SET value = ?, provenance = ?, confirmed = ?, updated_at = ?"
        " WHERE id = ? AND profile_id = ? AND learner_id = ?",
        (encoded, provenance.value, confirmed, now, existing["id"], profile, learner_id),
    )
    _record_revision(
        conn,
        profile=profile,
        learner_id=learner_id,
        context_id=context_id,
        field=field,
        previous_value=str(existing["value"]),
        previous_provenance=str(existing["provenance"]),
        new_value=encoded,
        provenance=provenance,
        change_reason=change_reason,
    )
    return {"field": field, "change": "revised"}


def _record_revision(
    conn: sqlite3.Connection,
    *,
    profile: str,
    learner_id: str,
    context_id: str,
    field: str,
    previous_value: str | None,
    previous_provenance: str | None,
    new_value: str,
    provenance: Provenance,
    change_reason: ChangeReason,
) -> None:
    conn.execute(
        "INSERT INTO context_revisions"
        " (id, context_id, learner_id, profile_id, field, previous_value,"
        "  previous_provenance, new_value, provenance, change_reason, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _new_id(),
            context_id,
            learner_id,
            profile,
            field,
            previous_value,
            previous_provenance,
            new_value,
            provenance.value,
            change_reason.value,
            _now(),
        ),
    )


def _values_as_candidates(rows: list[sqlite3.Row], source: str) -> list[Candidate]:
    out: list[Candidate] = []
    for row in rows:
        field = str(row["field"])
        if field not in CONTEXT_FIELDS:
            continue
        try:
            provenance = Provenance(str(row["provenance"]))
        except ValueError:  # pragma: no cover - only via manual DB edits
            continue
        out.append(
            Candidate(
                field=field,
                value=decode_value(field, str(row["value"])),
                provenance=provenance,
                source=source,
                recorded_at=str(row["updated_at"]),
            )
        )
    return out


def _values_as_json(rows: list[sqlite3.Row]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in rows:
        field = str(row["field"])
        out[field] = {
            "value": decode_value(field, str(row["value"])),
            "provenance": str(row["provenance"]),
            "confirmed": bool(row["confirmed"]),
            "recorded_at": str(row["updated_at"]),
        }
    return out


# ── Read ──────────────────────────────────────────────────────────────────


def get_context(
    *,
    learner_key: str,
    track_id: str | None = None,
    track_name: str | None = None,
    current_request: dict[str, Any] | None = None,
    include_memory_candidates: bool = False,
    config: LearningStudioConfig | None = None,
) -> dict[str, Any]:
    """Return a learner's context, with temporary and confirmed kept distinct.

    Track selection is never guessed. With several active tracks and nothing
    naming one, the result says ``ambiguous`` and lists them so the agent can
    ask, rather than picking the most recent and quietly being wrong.
    """
    config = config or load_config()
    learner_key = _validated_key(learner_key)
    request = _validated_request(current_request, config)
    profile = profile_id()

    storage.initialize(config)
    with storage.connect(config) as conn:
        learner_id = _find_learner(conn, profile, learner_key)
        if learner_id is None:
            # An unknown learner is not an error — it is someone's first
            # session, and the empty shape is the right answer. But a caller
            # who *named* a track must still be refused: returning "no track
            # here" as success would let a caller learn that a track ID is
            # unknown to this learner while a known learner gets a refusal,
            # and it would silently ignore what they actually asked for.
            if track_id or track_name:
                raise NotFoundError(NOT_FOUND_MESSAGE)
            return _empty_context(profile, request, config)

        with storage.transaction(conn):
            purge_expired(conn, profile, learner_id)

        tracks = _list_tracks(conn, profile, learner_id)
        selection = _select_track(conn, profile, learner_id, tracks, track_id, track_name)

        temporary_row = _temporary_context(conn, profile, learner_id)
        temporary_values = (
            _context_values(conn, profile, learner_id, str(temporary_row["id"]))
            if temporary_row and not _is_expired(temporary_row)
            else []
        )

        confirmed_values: list[sqlite3.Row] = []
        selected_id = selection.get("track_id")
        if selected_id:
            track_context = _track_context(conn, profile, learner_id, str(selected_id))
            if track_context:
                confirmed_values = _context_values(
                    conn, profile, learner_id, str(track_context["id"])
                )

        pool: list[Candidate] = []
        pool += candidates_from_request(request)
        pool += _values_as_candidates(confirmed_values, "track")
        pool += _values_as_candidates(temporary_values, "temporary")
        pool += candidates_from_config(config.profile_context, config.defaults)
        resolved = resolve(pool)

        objectives = _objectives_json(conn, profile, learner_id, selected_id)
        payload = {
            "ok": True,
            "profile_scope": profile,
            "tracks": [_track_json(row) for row in tracks],
            "track_selection": selection,
            "temporary_context": _values_as_json(temporary_values),
            "temporary_context_expires_at": (
                str(temporary_row["expires_at"]) if temporary_row and temporary_values else None
            ),
            "confirmed_context": _values_as_json(confirmed_values),
            "resolved_context": {
                field: value.to_json() for field, value in sorted(resolved.items())
            },
            "objectives": objectives,
            "precedence": [p.value for p in Provenance],
        }
        if include_memory_candidates:
            payload["memory_candidates"] = _candidates_json(conn, profile, learner_id)
        return payload


def _empty_context(
    profile: str, request: dict[str, Any], config: LearningStudioConfig
) -> dict[str, Any]:
    pool = candidates_from_request(request)
    pool += candidates_from_config(config.profile_context, config.defaults)
    resolved = resolve(pool)
    return {
        "ok": True,
        "profile_scope": profile,
        "tracks": [],
        "track_selection": {"mode": "none", "track_id": None},
        "temporary_context": {},
        "temporary_context_expires_at": None,
        "confirmed_context": {},
        "resolved_context": {field: value.to_json() for field, value in sorted(resolved.items())},
        "objectives": [],
        "precedence": [p.value for p in Provenance],
    }


def _select_track(
    conn: sqlite3.Connection,
    profile: str,
    learner_id: str,
    tracks: list[sqlite3.Row],
    track_id: str | None,
    track_name: str | None,
) -> dict[str, Any]:
    if track_id:
        row = _owned_track(conn, profile, learner_id, track_id)
        return {"mode": "requested_id", "track_id": str(row["id"]), "name": str(row["name"])}

    if track_name:
        row = _track_by_name(conn, profile, learner_id, validate_track_name(track_name))
        if row is None:
            raise NotFoundError(NOT_FOUND_MESSAGE)
        return {"mode": "requested_name", "track_id": str(row["id"]), "name": str(row["name"])}

    active = [row for row in tracks if str(row["status"]) == TrackStatus.ACTIVE.value]
    if not active:
        return {"mode": "none", "track_id": None}
    if len(active) == 1:
        row = active[0]
        return {
            "mode": "single_active_track",
            "track_id": str(row["id"]),
            "name": str(row["name"]),
        }
    return {
        "mode": "ambiguous",
        "track_id": None,
        "candidates": [{"track_id": str(row["id"]), "name": str(row["name"])} for row in active],
        "note": (
            "This learner has several active tracks. Ask which one they mean, or pass "
            "track_id. No track context was applied."
        ),
    }


def _track_json(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "track_id": str(row["id"]),
        "name": str(row["name"]),
        "status": str(row["status"]),
        "confirmed_at": str(row["confirmed_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _objectives_json(
    conn: sqlite3.Connection, profile: str, learner_id: str, track_id: str | None
) -> list[dict[str, Any]]:
    if not track_id:
        return []
    rows = conn.execute(
        "SELECT * FROM objectives WHERE profile_id = ? AND learner_id = ? AND track_id = ?"
        " ORDER BY created_at, id",
        (profile, learner_id, track_id),
    )
    return [
        {
            "objective_id": str(row["id"]),
            "track_id": str(row["track_id"]),
            "behavior": str(row["behavior"]),
            "condition": str(row["condition"]),
            "standard": str(row["standard"]),
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        for row in rows
    ]


def _candidates_json(
    conn: sqlite3.Connection, profile: str, learner_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM memory_candidates WHERE profile_id = ? AND learner_id = ?"
        " ORDER BY created_at, id",
        (profile, learner_id),
    )
    return [
        {
            "candidate_id": str(row["id"]),
            "category": str(row["category"]),
            "statement": str(row["statement"]),
            "evidence_summary": str(row["evidence_summary"]),
            "confidence": str(row["confidence"]),
            "durability": str(row["durability"]),
            "confirmation_state": str(row["confirmation_state"]),
            "recommended_action": str(row["recommended_action"]),
            "replaces": row["replaces"],
            "track_id": row["track_id"],
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


# ── Write ─────────────────────────────────────────────────────────────────


def save_context(
    *,
    learner_key: str,
    temporary_context: dict[str, Any] | None = None,
    evidence_context: dict[str, Any] | None = None,
    corrections: list[dict[str, Any]] | None = None,
    track: dict[str, Any] | None = None,
    objectives: list[dict[str, Any]] | None = None,
    memory_candidates: list[dict[str, Any]] | None = None,
    remember_accessibility_needs: bool = False,
    config: LearningStudioConfig | None = None,
) -> dict[str, Any]:
    """Save context, and report exactly what did and did not become durable.

    Everything happens in one transaction: a caller that creates a track,
    writes its context, and adds objectives either gets all three or none.
    """
    config = config or load_config()
    learner_key = _validated_key(learner_key)
    profile = profile_id()

    outcome: dict[str, Any] = {
        "temporary_context": [],
        "evidence": [],
        "corrections": [],
        "track": {"status": "not_requested"},
        "objectives": [],
        "memory_candidates": {"accepted": [], "rejected": []},
        "rejected": [],
    }

    storage.initialize(config)
    with storage.connect(config) as conn, storage.transaction(conn):
        learner_id = _get_or_create_learner(conn, profile, learner_key)
        purge_expired(conn, profile, learner_id)

        track_row = _save_track(conn, profile, learner_id, track, config, outcome)
        selected_track_id = str(track_row["id"]) if track_row is not None else None

        _save_temporary(
            conn, profile, learner_id, temporary_context, evidence_context, config, outcome
        )
        _save_corrections(
            conn,
            profile,
            learner_id,
            corrections,
            selected_track_id,
            config,
            outcome,
            remember_accessibility_needs,
        )
        _save_objectives(conn, profile, learner_id, objectives, selected_track_id, outcome)
        _save_candidates(
            conn,
            profile,
            learner_id,
            memory_candidates,
            selected_track_id,
            config,
            outcome,
            remember_accessibility_needs,
        )

    return {
        "ok": True,
        "profile_scope": profile,
        "outcome": outcome,
        # Stated on every response, because the one thing an agent must not
        # conclude from a successful save is that Hermes memory changed.
        "hermes_memory_updated": False,
        "note": (
            "Memory candidates are proposals for you to evaluate. This plugin does not "
            "read or write Hermes memory; only you can decide to do that."
        ),
    }


def _save_track(
    conn: sqlite3.Connection,
    profile: str,
    learner_id: str,
    track: dict[str, Any] | None,
    config: LearningStudioConfig,
    outcome: dict[str, Any],
) -> sqlite3.Row | None:
    """Create, update, archive, or withdraw a track — never silently.

    The confirmation flag is the whole point of this function. Without it, a
    caller gets a refusal and an explanation, and nothing durable is written.
    """
    if not track:
        return None

    track_id = track.get("track_id")
    status_raw = track.get("status")
    confirmed = track.get("confirmed", False)

    if track_id:
        row = _owned_track(conn, profile, learner_id, str(track_id))
        now = _now()
        if status_raw is not None:
            status = _validated_status(status_raw)
            conn.execute(
                "UPDATE tracks SET status = ?, updated_at = ?"
                " WHERE id = ? AND profile_id = ? AND learner_id = ?",
                (status.value, now, row["id"], profile, learner_id),
            )
        if "name" in track:
            name = validate_track_name(track["name"])
            _reject_duplicate_name(conn, profile, learner_id, name, str(row["id"]))
            conn.execute(
                "UPDATE tracks SET name = ?, updated_at = ?"
                " WHERE id = ? AND profile_id = ? AND learner_id = ?",
                (name, now, row["id"], profile, learner_id),
            )
        _write_track_context(conn, profile, learner_id, str(row["id"]), track, config, outcome)
        outcome["track"] = {
            "status": "updated",
            "track_id": str(row["id"]),
            "track_status": (
                _validated_status(status_raw).value
                if status_raw is not None
                else str(row["status"])
            ),
        }
        return _owned_track(conn, profile, learner_id, str(row["id"]))

    if confirmed is not True:
        outcome["track"] = {
            "status": "rejected",
            "reason": (
                "Creating an ongoing track requires explicit learner confirmation. Ask the "
                "learner whether they want sustained work on this, and pass confirmed=true "
                "only after they say yes. Nothing durable was created; the context you sent "
                "was kept as temporary."
            ),
        }
        outcome["rejected"].append("track_creation_without_confirmation")
        return None

    name = validate_track_name(track.get("name") or track.get("context", {}).get("track_name", ""))
    _reject_duplicate_name(conn, profile, learner_id, name, None)

    existing = conn.execute(
        "SELECT COUNT(*) AS n FROM tracks WHERE profile_id = ? AND learner_id = ?"
        " AND status = 'active'",
        (profile, learner_id),
    ).fetchone()
    if int(existing["n"]) >= config.max_tracks_per_learner:
        raise ValidationError(
            f"This learner already has {config.max_tracks_per_learner} active tracks, the "
            "configured maximum. Archive one before adding another."
        )

    now = _now()
    new_id = _new_id()
    conn.execute(
        "INSERT INTO tracks"
        " (id, learner_id, profile_id, name, status, confirmed_at, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
        (new_id, learner_id, profile, name, now, now, now),
    )
    _write_track_context(conn, profile, learner_id, new_id, track, config, outcome)
    outcome["track"] = {"status": "created", "track_id": new_id, "track_status": "active"}
    return _owned_track(conn, profile, learner_id, new_id)


def _reject_duplicate_name(
    conn: sqlite3.Connection, profile: str, learner_id: str, name: str, allow_id: str | None
) -> None:
    """One track must never silently overwrite another."""
    existing = _track_by_name(conn, profile, learner_id, name)
    if existing is not None and str(existing["id"]) != allow_id:
        raise ValidationError(
            f"This learner already has a track named {name!r}. Pass its track_id to update "
            "it, or choose a different name — tracks are never merged silently."
        )


def _write_track_context(
    conn: sqlite3.Connection,
    profile: str,
    learner_id: str,
    track_id: str,
    track: dict[str, Any],
    config: LearningStudioConfig,
    outcome: dict[str, Any],
) -> None:
    raw = track.get("context") or {}
    if not raw:
        return
    values = _validated_context(raw, config)

    context_id = _ensure_context(conn, profile, learner_id, ContextScope.TRACK, track_id, config)
    written = []
    for field, value in sorted(values.items()):
        if field in SESSION_ONLY_FIELDS:
            # Durable accessibility needs go through the consent gate in
            # _save_corrections, never through a bulk track write.
            outcome["rejected"].append(f"session_only_field_in_track_context:{field}")
            continue
        result = _write_value(
            conn,
            profile=profile,
            learner_id=learner_id,
            context_id=context_id,
            field=field,
            value=value,
            provenance=Provenance.CONFIRMED_TRACK,
            change_reason=ChangeReason.CONFIRMED_TRACK,
        )
        written.append(result)
    outcome["track_context"] = written


def _save_temporary(
    conn: sqlite3.Connection,
    profile: str,
    learner_id: str,
    temporary_context: dict[str, Any] | None,
    evidence_context: dict[str, Any] | None,
    config: LearningStudioConfig,
    outcome: dict[str, Any],
) -> None:
    """Write conversational context and evidence to the temporary store.

    Both land in the same expiring container, but with different provenance:
    what the learner said outranks what their answers implied, and the
    resolver relies on that distinction.
    """
    for payload, provenance, reason, key in (
        (
            temporary_context,
            Provenance.EXPLICIT_REQUEST,
            ChangeReason.EXPLICIT_REQUEST,
            "temporary_context",
        ),
        (evidence_context, Provenance.RECENT_EVIDENCE, ChangeReason.EVIDENCE, "evidence"),
    ):
        if not payload:
            continue
        values = _validated_context(payload, config)
        context_id = _ensure_context(
            conn, profile, learner_id, ContextScope.TEMPORARY, None, config
        )
        for field, value in sorted(values.items()):
            outcome[key].append(
                _write_value(
                    conn,
                    profile=profile,
                    learner_id=learner_id,
                    context_id=context_id,
                    field=field,
                    value=value,
                    provenance=provenance,
                    change_reason=reason,
                )
            )


def _save_corrections(
    conn: sqlite3.Connection,
    profile: str,
    learner_id: str,
    corrections: list[dict[str, Any]] | None,
    track_id: str | None,
    config: LearningStudioConfig,
    outcome: dict[str, Any],
    remember_accessibility: bool,
) -> None:
    """Apply explicit corrections, which supersede the value they correct.

    A correction aimed at a track is durable; one with no track corrects the
    temporary context. Either way the previous value is preserved as a
    revision — a correction is a change of mind, not an erasure of history.
    """
    for correction in corrections or []:
        field = correction.get("field")
        if field not in CONTEXT_FIELDS:
            raise ValidationError(f"unknown context field: {field!r}")
        value = validate_field_value(field, correction.get("value"), config.max_context_value_chars)

        target_track = correction.get("track_id") or track_id
        durable = bool(correction.get("durable", False)) and target_track is not None

        if field in SESSION_ONLY_FIELDS and durable:
            if not remember_accessibility:
                outcome["rejected"].append(f"accessibility_needs_not_persisted:{field}")
                outcome["corrections"].append(
                    {
                        "field": field,
                        "change": "session_only",
                        "reason": (
                            "Accessibility needs stay session-only unless the learner "
                            "explicitly asks you to remember them. Honour the need for this "
                            "session; it was not stored durably."
                        ),
                    }
                )
                durable = False
            elif not config.allow_durable_accessibility_needs:
                outcome["rejected"].append(f"accessibility_needs_blocked_by_policy:{field}")
                outcome["corrections"].append(
                    {
                        "field": field,
                        "change": "session_only",
                        "reason": (
                            "This profile is configured never to store accessibility needs "
                            "durably. The need still applies for this session."
                        ),
                    }
                )
                durable = False

        if durable:
            _owned_track(conn, profile, learner_id, str(target_track))
            context_id = _ensure_context(
                conn, profile, learner_id, ContextScope.TRACK, str(target_track), config
            )
        else:
            context_id = _ensure_context(
                conn, profile, learner_id, ContextScope.TEMPORARY, None, config
            )

        result = _write_value(
            conn,
            profile=profile,
            learner_id=learner_id,
            context_id=context_id,
            field=field,
            value=value,
            provenance=Provenance.EXPLICIT_CORRECTION,
            change_reason=ChangeReason.EXPLICIT_CORRECTION,
        )
        result["durable"] = durable
        outcome["corrections"].append(result)


def _save_objectives(
    conn: sqlite3.Connection,
    profile: str,
    learner_id: str,
    objectives: list[dict[str, Any]] | None,
    track_id: str | None,
    outcome: dict[str, Any],
) -> None:
    """Create or update objectives, refusing to mark one met on a single result.

    ``status: met`` needs ``confirm_met``: an objective's standard describes
    consistent performance, and one right answer is not that.
    """
    for objective in objectives or []:
        target_track = objective.get("track_id") or track_id
        if not target_track:
            outcome["rejected"].append("objective_without_track")
            outcome["objectives"].append(
                {
                    "status": "rejected",
                    "reason": (
                        "An objective belongs to a confirmed track. Confirm a track first, "
                        "or pass track_id."
                    ),
                }
            )
            continue
        _owned_track(conn, profile, learner_id, str(target_track))

        status = _validated_objective_status(objective.get("status", "active"))
        if status is ObjectiveStatus.MET and objective.get("confirm_met") is not True:
            outcome["rejected"].append("objective_met_without_confirmation")
            outcome["objectives"].append(
                {
                    "status": "rejected",
                    "reason": (
                        "An objective is met when the learner performs to its stated "
                        "standard consistently, not on one answer or one exercise. Pass "
                        "confirm_met=true only when the standard has actually been met."
                    ),
                }
            )
            continue

        now = _now()
        objective_id = objective.get("objective_id")
        if objective_id:
            row = conn.execute(
                "SELECT * FROM objectives WHERE id = ? AND profile_id = ? AND learner_id = ?",
                (str(objective_id), profile, learner_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("No such objective for this learner.")
            conn.execute(
                "UPDATE objectives SET behavior = ?, condition = ?, standard = ?,"
                " status = ?, updated_at = ?"
                " WHERE id = ? AND profile_id = ? AND learner_id = ?",
                (
                    _text(objective.get("behavior", row["behavior"]), "behavior"),
                    _text(objective.get("condition", row["condition"]), "condition"),
                    _text(objective.get("standard", row["standard"]), "standard"),
                    status.value,
                    now,
                    str(objective_id),
                    profile,
                    learner_id,
                ),
            )
            outcome["objectives"].append({"status": "updated", "objective_id": str(objective_id)})
            continue

        new_id = _new_id()
        conn.execute(
            "INSERT INTO objectives"
            " (id, track_id, learner_id, profile_id, behavior, condition, standard,"
            "  status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id,
                str(target_track),
                learner_id,
                profile,
                _text(objective.get("behavior"), "behavior"),
                _text(objective.get("condition"), "condition"),
                _text(objective.get("standard"), "standard"),
                status.value,
                now,
                now,
            ),
        )
        outcome["objectives"].append({"status": "created", "objective_id": new_id})


def _save_candidates(
    conn: sqlite3.Connection,
    profile: str,
    learner_id: str,
    proposals: list[dict[str, Any]] | None,
    track_id: str | None,
    config: LearningStudioConfig,
    outcome: dict[str, Any],
    remember_accessibility: bool,
) -> None:
    """Validate proposals and store the survivors.

    A rejected proposal is reported with its reason rather than dropped: the
    agent needs to know *why* something is not memory material, otherwise it
    will propose the same thing again next turn.
    """
    permitted = remember_accessibility and config.allow_durable_accessibility_needs

    for proposal in proposals or []:
        try:
            candidate = candidate_rules.propose(
                category=proposal.get("category"),
                statement=proposal.get("statement"),
                evidence_summary=proposal.get("evidence_summary"),
                origin=proposal.get("origin"),
                recommended_action=proposal.get("recommended_action", "add"),
                confidence=proposal.get("confidence", "medium"),
                durability=proposal.get("durability", "durable"),
                confirmation_state=proposal.get("confirmation_state", "unconfirmed"),
                replaces=proposal.get("replaces"),
                track_id=str(proposal.get("track_id") or track_id or "") or None,
                evidence_count=int(proposal.get("evidence_count", 1)),
                min_evidence=config.memory_candidate_min_evidence,
                learner_permitted_accessibility=permitted,
            )
        except (candidate_rules.CandidateRejected, ValueError, TypeError) as exc:
            outcome["memory_candidates"]["rejected"].append(
                {"statement": _safe_echo(proposal.get("statement")), "reason": str(exc)}
            )
            continue

        if candidate.track_id:
            _owned_track(conn, profile, learner_id, candidate.track_id)

        candidate_id = _new_id()
        timestamp = _now()
        conn.execute(
            "INSERT INTO memory_candidates"
            " (id, learner_id, profile_id, track_id, category, statement, evidence_summary,"
            "  confidence, durability, confirmation_state, recommended_action, replaces,"
            "  created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate_id,
                learner_id,
                profile,
                candidate.track_id,
                candidate.category.value,
                candidate.statement,
                candidate.evidence_summary,
                candidate.confidence.value,
                candidate.durability.value,
                candidate.confirmation_state.value,
                candidate.recommended_action.value,
                candidate.replaces,
                timestamp,
                timestamp,
            ),
        )
        accepted = candidate.to_json()
        accepted["candidate_id"] = candidate_id
        outcome["memory_candidates"]["accepted"].append(accepted)


def _safe_echo(value: Any) -> str:
    """Echo a rejected statement back, truncated, so the agent can identify it."""
    text = str(value or "")
    return text[:80] + ("…" if len(text) > 80 else "")


# ── Input validation helpers ──────────────────────────────────────────────


def _validated_key(raw: Any) -> str:
    try:
        return validate_learner_key(raw)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def _validated_context(raw: Any, config: LearningStudioConfig) -> dict[str, Any]:
    try:
        return validate_context_payload(raw, config.max_context_value_chars)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def _validated_request(raw: Any, config: LearningStudioConfig) -> dict[str, Any]:
    if not raw:
        return {}
    return _validated_context(raw, config)


def _validated_status(raw: Any) -> TrackStatus:
    try:
        return TrackStatus(raw)
    except ValueError:
        allowed = ", ".join(s.value for s in TrackStatus)
        raise ValidationError(f"track status must be one of: {allowed}") from None


def _validated_objective_status(raw: Any) -> ObjectiveStatus:
    try:
        return ObjectiveStatus(raw)
    except ValueError:
        allowed = ", ".join(s.value for s in ObjectiveStatus)
        raise ValidationError(f"objective status must be one of: {allowed}") from None


def _text(raw: Any, label: str) -> str:
    from .models import MAX_VALUE_CHARS, clean_text

    try:
        return clean_text(raw, label, MAX_VALUE_CHARS)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
