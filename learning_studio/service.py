"""The Learning Studio service: reads, writes, and the rules that gate them.

This is where the plugin's promises are actually kept, so the rules are worth
stating before the code:

**Identity comes from the host, never from the model.** Every entry point
takes a :class:`~learning_studio.identity.Principal` resolved from Hermes'
session context. No tool argument names a learner, so there is nothing to
impersonate with.

**Isolation is enforced here, not in the tool handler.** Every learner-owned
query carries ``profile_id`` and ``learner_id`` in its ``WHERE`` clause, and
composite foreign keys make a mismatched row unstorable in the first place.

**Nothing becomes durable by accident.** A track is created only with an
explicit confirmation flag. Sensitive fields need consent bound to the
specific fact. Repetition and agent confidence are not consent.

**Not-found and not-yours are the same answer.** Distinguishing them would
turn a track ID into an oracle for whether another learner exists.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from . import candidates as candidate_rules
from . import storage
from .config import LearningStudioConfig, load_config
from .context import Candidate, candidates_from_config, candidates_from_request, resolve
from .identity import Principal
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
    validate_track_name,
)

#: Returned whenever a caller names an object they do not own, or one that
#: does not exist. The two cases are deliberately indistinguishable.
NOT_FOUND_MESSAGE = "No such track for this learner."

NOT_FOUND_OBJECTIVE_MESSAGE = "No such objective on that track for this learner."

NOT_FOUND_EXPERIENCE_MESSAGE = "No such prepared exercise for this learner."

#: Track states that accept no new context, objectives, or corrections. A
#: learner who archived or withdrew a track has said they are done with it;
#: quietly writing to it anyway would undo that.
CLOSED_STATUSES = (TrackStatus.ARCHIVED, TrackStatus.WITHDRAWN)


class ServiceError(Exception):
    """A request was refused. The message is safe to show the agent."""


class ValidationError(ServiceError):
    """Input failed validation."""


class NotFoundError(ServiceError):
    """The object does not exist, or is not this learner's. Never say which."""


class ConsentError(ServiceError):
    """A sensitive write was attempted without consent for that specific fact."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


# ── Learner identity ──────────────────────────────────────────────────────


def _learner_salt(conn: sqlite3.Connection) -> str:
    """Return this database's digest salt, creating it on first use.

    **What this does and does not buy.** The salt makes the stored digests
    useless against a *precomputed* table, and keeps the platform ID out of
    casual view — logs, backups, a glance at the file. It does **not** make
    identity unrecoverable: the salt lives in this same database, so anyone
    holding the file can brute-force the low-entropy space of platform user
    IDs offline. Resisting that would need a pepper stored outside the
    database, with its own lifecycle and rotation story, and this plugin does
    not create secrets on the user's behalf.

    The digest is a lookup key, never an authorisation check. Authorisation
    is :func:`learning_studio.identity.resolve_principal`.
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


def _digest(conn: sqlite3.Connection, principal: Principal) -> str:
    """Derive the lookup digest for an authenticated principal."""
    salt = _learner_salt(conn)
    material = f"{principal.profile}\x00{principal.scope}".encode()
    return hmac.new(salt.encode("utf-8"), material, hashlib.sha256).hexdigest()


def _get_or_create_learner(conn: sqlite3.Connection, principal: Principal) -> str:
    digest = _digest(conn, principal)
    row = conn.execute(
        "SELECT id FROM learners WHERE profile_id = ? AND principal_digest = ?",
        (principal.profile, digest),
    ).fetchone()
    if row:
        return str(row["id"])

    learner_id = _new_id()
    now = _now()
    conn.execute(
        "INSERT INTO learners"
        " (id, profile_id, principal_digest, platform, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (learner_id, principal.profile, digest, principal.platform, now, now),
    )
    return learner_id


def _find_learner(conn: sqlite3.Connection, principal: Principal) -> str | None:
    row = conn.execute(
        "SELECT id FROM learners WHERE profile_id = ? AND principal_digest = ?",
        (principal.profile, _digest(conn, principal)),
    ).fetchone()
    return str(row["id"]) if row else None


# ── Consent for session-only fields ───────────────────────────────────────


class AccessibilityConsent:
    """What the learner agreed to have remembered, and in whose words.

    Deliberately not a boolean. A single ``True`` was accepted as blanket
    permission for whatever sensitive value happened to be in the same
    request, which is not what anyone means when they say "yes, remember
    that I need captions". Consent names the specific facts.
    """

    __slots__ = ("statement", "needs")

    def __init__(self, statement: str, needs: frozenset[str]) -> None:
        self.statement = statement
        self.needs = needs

    def covers(self, value: Any) -> bool:
        """True when every part of *value* is a need the learner listed.

        Compared on the canonical form, so "Captions On Audio" and
        "captions on audio" are the same need while "captions" is not the
        same as "captions on all video" — see
        :func:`~learning_studio.models.normalize_need`.
        """
        from .models import normalize_need

        values = value if isinstance(value, list) else [value]
        if not values:
            return False
        try:
            return all(normalize_need(item) in self.needs for item in values)
        except ValueError:
            return False

    @classmethod
    def parse(cls, raw: Any, config: LearningStudioConfig) -> AccessibilityConsent | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValidationError("accessibility_consent must be an object")
        unknown = set(raw) - {"consent_statement", "needs"}
        if unknown:
            raise ValidationError(
                f"unknown accessibility_consent field(s): {', '.join(sorted(unknown))}"
            )
        if not config.allow_durable_accessibility_needs:
            raise ConsentError(
                "This profile is configured never to store accessibility needs durably. "
                "The need still applies for this session; nothing was stored."
            )
        from .models import clean_text

        try:
            statement = clean_text(
                raw.get("consent_statement"), "consent_statement", config.max_context_value_chars
            )
            needs = validate_field_value(
                "accessibility_needs", raw.get("needs"), config.max_context_value_chars
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        from .models import normalize_need

        try:
            canonical = frozenset(normalize_need(item) for item in needs)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return cls(statement=statement, needs=canonical)


def _consent_allows(field: str, value: Any, consent: AccessibilityConsent | None) -> bool:
    """Whether this value may be written to disk. For a need, never.

    The vocabulary restriction that used to sit here stopped a *diagnosis*
    from being stored — a real gain — but it never answered the question it
    appeared to. ``accessibility_consent`` is written by the model, in the
    same call as the need it authorises, and so is ``track.confirmed``. One
    request could therefore create a row reading ``accessibility_needs =
    captions, provenance = confirmed_track, confirmed = 1``, and a later
    manifest could cite that row as proof the learner had agreed. Nothing
    outside the model was involved at any point.

    Hermes exposes no consent event to check against, so the honest answer is
    that this plugin cannot record an accessibility need at all. It is
    honoured for the call that carries it, and nowhere else.
    """
    del consent, value
    return field not in SESSION_ONLY_FIELDS


SESSION_ONLY_NOT_STORED = (
    "Accessibility needs are session-only and are never written to storage. This one was "
    "honoured for the current request and will not be available in a later call: pass it in "
    "current_request each session. There is no argument that changes this. Consent, a "
    "confirmed track, and the need itself are all things you write in the same call, so "
    "none of them can show that the learner agreed to a durable record, and this plugin "
    "will not create one that says otherwise."
)


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


def _writable_track(
    conn: sqlite3.Connection, profile: str, learner_id: str, track_id: str
) -> sqlite3.Row:
    """Fetch a track that may still receive writes."""
    row = _owned_track(conn, profile, learner_id, track_id)
    status = TrackStatus(str(row["status"]))
    if status in CLOSED_STATUSES:
        raise ValidationError(
            f"That track is {status.value}. The learner set it aside, so it takes no new "
            "context or objectives. Ask whether they want to reactivate it, and send "
            "status='active' first if they do."
        )
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


def purge_expired(conn: sqlite3.Connection, profile: str, limit: int = 500) -> int:
    """Physically delete expired temporary contexts across the whole profile.

    Profile-wide rather than per-learner, because the learner whose data
    expired is exactly the one who has not come back. Scoping cleanup to the
    caller meant abandoned context lived on disk indefinitely — a retention
    promise the plugin was not actually keeping.

    Cascades to values and revisions. Confirmed tracks are untouched: the
    ``scope = 'temporary'`` predicate cannot match a track context, which the
    schema's CHECK constraint guarantees carries no expiry at all. Bounded so
    a backlog cannot turn one tool call into an unbounded delete.
    """
    rows = conn.execute(
        "SELECT id FROM learning_contexts"
        " WHERE profile_id = ? AND scope = 'temporary'"
        "   AND expires_at IS NOT NULL AND expires_at <= ?"
        " LIMIT ?",
        (profile, _now(), limit),
    ).fetchall()
    if not rows:
        return 0
    ids = [str(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"DELETE FROM learning_contexts WHERE id IN ({placeholders}) AND profile_id = ?",
        (*ids, profile),
    )
    return len(ids)


def _context_values(
    conn: sqlite3.Connection, profile: str, learner_id: str, context_id: str
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM context_values"
            " WHERE profile_id = ? AND learner_id = ? AND context_id = ?"
            " ORDER BY field, provenance",
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
    """Upsert one context value, keyed by field **and provenance**.

    Keying on provenance is the fix for the defect where evidence destroyed
    an explicit statement. Previously all sources shared one row per field,
    so "the learner said X" and "their answers suggest Y" collided and the
    later write won regardless of authority. Now they coexist as separate
    candidates and :mod:`learning_studio.context` decides between them, which
    is the only place that decision belongs.

    Revisions record the structured before/after and why — never the
    conversation that produced it.
    """
    encoded = encode_value(value)
    now = _now()
    existing = conn.execute(
        "SELECT * FROM context_values"
        " WHERE context_id = ? AND field = ? AND provenance = ?"
        "   AND profile_id = ? AND learner_id = ?",
        (context_id, field, provenance.value, profile, learner_id),
    ).fetchone()

    confirmed = 1 if provenance in DURABLE_WRITE_PROVENANCES else 0

    if existing is None:
        # A new row for this provenance does not mean the field had no value.
        # The revision log tracks what the field's *effective* value was, so a
        # correction still records the statement it supersedes even though the
        # two now live in separate rows.
        superseded = _effective_value(conn, profile, learner_id, context_id, field)
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
            previous_value=superseded[0],
            previous_provenance=superseded[1],
            new_value=encoded,
            provenance=provenance,
            change_reason=change_reason,
        )
        return {"field": field, "provenance": provenance.value, "change": "created"}

    if existing["value"] == encoded:
        return {"field": field, "provenance": provenance.value, "change": "unchanged"}

    conn.execute(
        "UPDATE context_values SET value = ?, confirmed = ?, updated_at = ?"
        " WHERE id = ? AND profile_id = ? AND learner_id = ?",
        (encoded, confirmed, now, existing["id"], profile, learner_id),
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
    return {"field": field, "provenance": provenance.value, "change": "revised"}


def _effective_value(
    conn: sqlite3.Connection, profile: str, learner_id: str, context_id: str, field: str
) -> tuple[str | None, str | None]:
    """Return the field's current winning ``(value, provenance)`` in this context."""
    from .context import Candidate

    rows = conn.execute(
        "SELECT value, provenance, updated_at FROM context_values"
        " WHERE context_id = ? AND field = ? AND profile_id = ? AND learner_id = ?",
        (context_id, field, profile, learner_id),
    ).fetchall()
    best: tuple[Any, sqlite3.Row] | None = None
    for row in rows:
        try:
            provenance = Provenance(str(row["provenance"]))
        except ValueError:  # pragma: no cover - only via manual DB edits
            continue
        key = Candidate(
            field=field,
            value=None,
            provenance=provenance,
            source="stored",
            recorded_at=str(row["updated_at"]),
        )._sort_key()
        if best is None or key < best[0]:
            best = (key, row)
    if best is None:
        return None, None
    return str(best[1]["value"]), str(best[1]["provenance"])


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
    """Group stored values by field, best-supported first.

    A field can now hold several values with different provenance, so this
    returns the strongest as the field's entry and the rest alongside it —
    the alternative, picking one and dropping the others, is the bug this
    schema change exists to fix.
    """
    from .models import PRECEDENCE

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        field = str(row["field"])
        try:
            provenance = Provenance(str(row["provenance"]))
        except ValueError:  # pragma: no cover
            continue
        grouped.setdefault(field, []).append(
            {
                "value": decode_value(field, str(row["value"])),
                "provenance": provenance.value,
                "confirmed": bool(row["confirmed"]),
                "recorded_at": str(row["updated_at"]),
                "_rank": PRECEDENCE[provenance],
            }
        )

    out: dict[str, Any] = {}
    for field, entries in grouped.items():
        entries.sort(key=lambda e: (e["_rank"], e["recorded_at"]))
        best = dict(entries[0])
        best.pop("_rank")
        others = []
        for entry in entries[1:]:
            other = dict(entry)
            other.pop("_rank")
            others.append(other)
        if others:
            best["also_recorded"] = others
        out[field] = best
    return out


# ── Read ──────────────────────────────────────────────────────────────────


def get_context(
    *,
    principal: Principal,
    track_id: str | None = None,
    track_name: str | None = None,
    current_request: dict[str, Any] | None = None,
    include_memory_candidates: bool = False,
    config: LearningStudioConfig | None = None,
) -> dict[str, Any]:
    """Return this principal's context, temporary and confirmed kept distinct.

    Track selection is never guessed. With several active tracks and nothing
    naming one, the result says ``ambiguous`` and lists them so the agent can
    ask, rather than picking the most recent and quietly being wrong.
    """
    config = config or load_config()
    request = _validated_request(current_request, config)
    profile = principal.profile

    storage.initialize(config)
    with storage.connect(config) as conn:
        with storage.transaction(conn):
            purge_expired(conn, profile)
            purge_expired_candidates(conn, profile)

        learner_id = _find_learner(conn, principal)
        if learner_id is None:
            # An unknown learner is not an error — it is someone's first
            # session. But a caller who *named* a track must still be
            # refused, or "no track here" becomes a success signal that a
            # known learner would have been denied.
            if track_id or track_name:
                raise NotFoundError(NOT_FOUND_MESSAGE)
            return _empty_context(principal, request, config, include_memory_candidates)

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

        payload = {
            "ok": True,
            "learner": principal.describe(),
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
            "objectives": _objectives_json(conn, profile, learner_id, selected_id),
            "precedence": [p.value for p in Provenance],
        }
        if include_memory_candidates:
            payload["memory_candidates"] = _candidates_json(conn, profile, learner_id)
        return payload


def _empty_context(
    principal: Principal,
    request: dict[str, Any],
    config: LearningStudioConfig,
    include_memory_candidates: bool = False,
) -> dict[str, Any]:
    """The shape a caller gets on someone's first session.

    Keys must not depend on whether the learner happens to exist yet: a
    caller that asked for memory candidates gets the key either way, empty.
    """
    pool = candidates_from_request(request)
    pool += candidates_from_config(config.profile_context, config.defaults)
    resolved = resolve(pool)
    return {
        "ok": True,
        "learner": principal.describe(),
        "tracks": [],
        "track_selection": {"mode": "none", "track_id": None},
        "temporary_context": {},
        "temporary_context_expires_at": None,
        "confirmed_context": {},
        "resolved_context": {field: value.to_json() for field, value in sorted(resolved.items())},
        "objectives": [],
        "precedence": [p.value for p in Provenance],
        **({"memory_candidates": []} if include_memory_candidates else {}),
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
        return {"mode": "single_active_track", "track_id": str(row["id"]), "name": str(row["name"])}
    return {
        "mode": "ambiguous",
        "track_id": None,
        "candidates": [{"track_id": str(r["id"]), "name": str(r["name"])} for r in active],
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
    """Return stored candidates with the provenance needed to judge them.

    ``origin``, ``evidence_count``, and ``consent_reference`` come back
    because a candidate that cannot be audited later cannot responsibly be
    acted on later — the agent reading this in a fortnight has to be able to
    tell a learner's stated preference from a pattern someone noticed.
    """
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
            "origin": str(row["origin"]),
            "evidence_count": row["evidence_count"],
            "confidence": str(row["confidence"]),
            "durability": str(row["durability"]),
            "confirmation_state": str(row["confirmation_state"]),
            "recommended_action": str(row["recommended_action"]),
            "replaces": row["replaces"],
            "consent_reference": row["consent_reference"],
            "consented_need": row["consented_need"],
            "expires_at": row["expires_at"],
            "track_id": row["track_id"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        for row in rows
    ]


# ── Write ─────────────────────────────────────────────────────────────────


def save_context(
    *,
    principal: Principal,
    temporary_context: dict[str, Any] | None = None,
    evidence_context: dict[str, Any] | None = None,
    corrections: list[dict[str, Any]] | None = None,
    track: dict[str, Any] | None = None,
    objectives: list[dict[str, Any]] | None = None,
    memory_candidates: list[dict[str, Any]] | None = None,
    accessibility_consent: dict[str, Any] | None = None,
    config: LearningStudioConfig | None = None,
) -> dict[str, Any]:
    """Save context, and report exactly what did and did not become durable.

    Everything happens in one transaction: a caller that creates a track,
    writes its context, and adds objectives either gets all three or none.
    """
    config = config or load_config()
    profile = principal.profile
    consent = AccessibilityConsent.parse(accessibility_consent, config)

    outcome: dict[str, Any] = {
        "temporary_context": [],
        "evidence": [],
        "corrections": [],
        "track": {"status": "not_requested"},
        "objectives": [],
        "memory_candidates": {"accepted": [], "rejected": []},
        "not_stored": [],
        "rejected": [],
    }

    storage.initialize(config)
    with storage.connect(config) as conn, storage.transaction(conn):
        purge_expired(conn, profile)
        purge_expired_candidates(conn, profile)
        learner_id = _get_or_create_learner(conn, principal)

        track_row = _save_track(conn, profile, learner_id, track, config, outcome, consent)
        selected_track_id = str(track_row["id"]) if track_row is not None else None

        _save_temporary(
            conn,
            profile,
            learner_id,
            temporary_context,
            evidence_context,
            config,
            outcome,
            consent,
        )
        _save_corrections(
            conn,
            profile,
            learner_id,
            corrections,
            selected_track_id,
            config,
            outcome,
            consent,
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
            consent,
        )

    return {
        "ok": True,
        "learner": principal.describe(),
        "outcome": outcome,
        # Stated on every response, because the one thing an agent must not
        # conclude from a successful save is that Hermes memory changed.
        "hermes_memory_updated": False,
        "note": (
            "Memory candidates are proposals for you to evaluate. This plugin does not "
            "read or write Hermes memory; only you can decide to do that."
        ),
    }


def _write_context_values(
    conn: sqlite3.Connection,
    *,
    profile: str,
    learner_id: str,
    context_id: str,
    values: dict[str, Any],
    provenance: Provenance,
    change_reason: ChangeReason,
    consent: AccessibilityConsent | None,
    outcome: dict[str, Any],
    results: list[dict[str, Any]],
) -> None:
    """Write a validated mapping, skipping session-only fields without consent."""
    for field, value in sorted(values.items()):
        if not _consent_allows(field, value, consent):
            outcome["not_stored"].append({"field": field, "reason": SESSION_ONLY_NOT_STORED})
            continue
        results.append(
            _write_value(
                conn,
                profile=profile,
                learner_id=learner_id,
                context_id=context_id,
                field=field,
                value=value,
                provenance=provenance,
                change_reason=change_reason,
            )
        )


def _save_track(
    conn: sqlite3.Connection,
    profile: str,
    learner_id: str,
    track: dict[str, Any] | None,
    config: LearningStudioConfig,
    outcome: dict[str, Any],
    consent: AccessibilityConsent | None,
) -> sqlite3.Row | None:
    """Create, update, archive, or withdraw a track — never silently."""
    if not track:
        return None

    track_id = track.get("track_id")
    status_raw = track.get("status")
    confirmed = track.get("confirmed", False)

    if track_id:
        return _update_track(
            conn, profile, learner_id, str(track_id), track, status_raw, config, outcome, consent
        )

    if confirmed is not True:
        # No durable track. The context that came with it is not discarded:
        # it is kept as temporary, which is what the response says happens
        # and what makes a one-off request still useful.
        _keep_rejected_track_context(conn, profile, learner_id, track, config, outcome, consent)
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

    active = conn.execute(
        "SELECT COUNT(*) AS n FROM tracks"
        " WHERE profile_id = ? AND learner_id = ? AND status = 'active'",
        (profile, learner_id),
    ).fetchone()
    if int(active["n"]) >= config.max_tracks_per_learner:
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
    _write_track_context(conn, profile, learner_id, new_id, track, config, outcome, consent)
    outcome["track"] = {"status": "created", "track_id": new_id, "track_status": "active"}
    return _owned_track(conn, profile, learner_id, new_id)


def _update_track(
    conn: sqlite3.Connection,
    profile: str,
    learner_id: str,
    track_id: str,
    track: dict[str, Any],
    status_raw: Any,
    config: LearningStudioConfig,
    outcome: dict[str, Any],
    consent: AccessibilityConsent | None,
) -> sqlite3.Row:
    row = _owned_track(conn, profile, learner_id, track_id)
    now = _now()
    new_status = _validated_status(status_raw) if status_raw is not None else None

    if new_status is not None:
        conn.execute(
            "UPDATE tracks SET status = ?, updated_at = ?"
            " WHERE id = ? AND profile_id = ? AND learner_id = ?",
            (new_status.value, now, track_id, profile, learner_id),
        )

    if "name" in track:
        name = validate_track_name(track["name"])
        _reject_duplicate_name(conn, profile, learner_id, name, track_id)
        conn.execute(
            "UPDATE tracks SET name = ?, updated_at = ?"
            " WHERE id = ? AND profile_id = ? AND learner_id = ?",
            (name, now, track_id, profile, learner_id),
        )

    if track.get("context"):
        # Reject context on a closed track *unless* this same call reopened
        # it, so "reactivate and continue" is one request rather than two.
        effective = new_status or TrackStatus(str(row["status"]))
        if effective in CLOSED_STATUSES:
            raise ValidationError(
                f"That track is {effective.value}. The learner set it aside, so it takes no "
                "new context. Ask whether they want to reactivate it, and send "
                "status='active' in the same call if they do."
            )
        _write_track_context(conn, profile, learner_id, track_id, track, config, outcome, consent)

    outcome["track"] = {
        "status": "updated",
        "track_id": track_id,
        "track_status": (new_status.value if new_status else str(row["status"])),
    }
    return _owned_track(conn, profile, learner_id, track_id)


def _keep_rejected_track_context(
    conn: sqlite3.Connection,
    profile: str,
    learner_id: str,
    track: dict[str, Any],
    config: LearningStudioConfig,
    outcome: dict[str, Any],
    consent: AccessibilityConsent | None,
) -> None:
    """Retain an unconfirmed track's context as temporary, as promised."""
    raw = track.get("context") or {}
    if not raw:
        return
    values = _validated_context(raw, config)
    context_id = _ensure_context(conn, profile, learner_id, ContextScope.TEMPORARY, None, config)
    _write_context_values(
        conn,
        profile=profile,
        learner_id=learner_id,
        context_id=context_id,
        values=values,
        provenance=Provenance.EXPLICIT_REQUEST,
        change_reason=ChangeReason.EXPLICIT_REQUEST,
        consent=consent,
        outcome=outcome,
        results=outcome["temporary_context"],
    )


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
    consent: AccessibilityConsent | None,
) -> None:
    raw = track.get("context") or {}
    if not raw:
        return
    values = _validated_context(raw, config)
    context_id = _ensure_context(conn, profile, learner_id, ContextScope.TRACK, track_id, config)
    written: list[dict[str, Any]] = []
    _write_context_values(
        conn,
        profile=profile,
        learner_id=learner_id,
        context_id=context_id,
        values=values,
        provenance=Provenance.CONFIRMED_TRACK,
        change_reason=ChangeReason.CONFIRMED_TRACK,
        consent=consent,
        outcome=outcome,
        results=written,
    )
    outcome["track_context"] = written


def _save_temporary(
    conn: sqlite3.Connection,
    profile: str,
    learner_id: str,
    temporary_context: dict[str, Any] | None,
    evidence_context: dict[str, Any] | None,
    config: LearningStudioConfig,
    outcome: dict[str, Any],
    consent: AccessibilityConsent | None,
) -> None:
    """Write conversational context and evidence to the temporary store.

    Both land in the same expiring container but with different provenance
    and, since the schema keys on provenance, in different rows. What the
    learner said and what their answers implied can now disagree without
    either erasing the other.
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
        _write_context_values(
            conn,
            profile=profile,
            learner_id=learner_id,
            context_id=context_id,
            values=values,
            provenance=provenance,
            change_reason=reason,
            consent=consent,
            outcome=outcome,
            results=outcome[key],
        )


def _save_corrections(
    conn: sqlite3.Connection,
    profile: str,
    learner_id: str,
    corrections: list[dict[str, Any]] | None,
    track_id: str | None,
    config: LearningStudioConfig,
    outcome: dict[str, Any],
    consent: AccessibilityConsent | None,
) -> None:
    """Apply explicit corrections, which supersede the value they correct."""
    for correction in corrections or []:
        field = correction.get("field")
        if field not in CONTEXT_FIELDS:
            raise ValidationError(f"unknown context field: {field!r}")
        value = validate_field_value(field, correction.get("value"), config.max_context_value_chars)

        target_track = correction.get("track_id") or track_id
        durable = bool(correction.get("durable", False)) and target_track is not None

        if not _consent_allows(field, value, consent):
            outcome["not_stored"].append({"field": field, "reason": SESSION_ONLY_NOT_STORED})
            outcome["corrections"].append({"field": field, "change": "not_stored"})
            continue

        if durable:
            _writable_track(conn, profile, learner_id, str(target_track))
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
    """Create or update objectives, refusing to mark one met on a single result."""
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
        _writable_track(conn, profile, learner_id, str(target_track))

        objective_id = objective.get("objective_id")
        if objective_id:
            _update_objective(
                conn,
                profile,
                learner_id,
                str(target_track),
                str(objective_id),
                objective,
                outcome,
            )
            continue

        status = _validated_objective_status(objective.get("status", "active"))
        if not _met_allowed(status, objective, outcome):
            continue

        now = _now()
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


def _update_objective(
    conn: sqlite3.Connection,
    profile: str,
    learner_id: str,
    track_id: str,
    objective_id: str,
    objective: dict[str, Any],
    outcome: dict[str, Any],
) -> None:
    """Update one objective, bound to its track as well as its owner.

    The track is part of the lookup, not merely validated alongside it. With
    only the objective ID scoped, a caller could pass objective A from track
    X together with track Y and silently edit A — the ownership check passed
    because the learner owned both.
    """
    row = conn.execute(
        "SELECT * FROM objectives"
        " WHERE id = ? AND track_id = ? AND profile_id = ? AND learner_id = ?",
        (objective_id, track_id, profile, learner_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(NOT_FOUND_OBJECTIVE_MESSAGE)

    # Every omitted field keeps its stored value. Defaulting status to
    # "active" silently reactivated objectives the learner had already met
    # or retired, which is the opposite of what leaving it out means.
    if "status" in objective:
        status = _validated_objective_status(objective["status"])
        if not _met_allowed(status, objective, outcome):
            return
    else:
        status = ObjectiveStatus(str(row["status"]))

    conn.execute(
        "UPDATE objectives SET behavior = ?, condition = ?, standard = ?,"
        " status = ?, updated_at = ?"
        " WHERE id = ? AND track_id = ? AND profile_id = ? AND learner_id = ?",
        (
            _text(objective.get("behavior", row["behavior"]), "behavior"),
            _text(objective.get("condition", row["condition"]), "condition"),
            _text(objective.get("standard", row["standard"]), "standard"),
            status.value,
            _now(),
            objective_id,
            track_id,
            profile,
            learner_id,
        ),
    )
    outcome["objectives"].append({"status": "updated", "objective_id": objective_id})


def _met_allowed(
    status: ObjectiveStatus, objective: dict[str, Any], outcome: dict[str, Any]
) -> bool:
    """An objective is met by consistent performance, never one right answer."""
    if status is not ObjectiveStatus.MET or objective.get("confirm_met") is True:
        return True
    outcome["rejected"].append("objective_met_without_confirmation")
    outcome["objectives"].append(
        {
            "status": "rejected",
            "reason": (
                "An objective is met when the learner performs to its stated standard "
                "consistently, not on one answer or one exercise. Pass confirm_met=true "
                "only when the standard has actually been met."
            ),
        }
    )
    return False


def _save_candidates(
    conn: sqlite3.Connection,
    profile: str,
    learner_id: str,
    proposals: list[dict[str, Any]] | None,
    track_id: str | None,
    config: LearningStudioConfig,
    outcome: dict[str, Any],
    consent: AccessibilityConsent | None,
) -> None:
    """Validate proposals and store the survivors, with their provenance."""
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
                consent_statement=consent.statement if consent else None,
                consented_needs=consent.needs if consent else frozenset(),
                consented_need=proposal.get("consented_need"),
            )
        except (candidate_rules.CandidateRejected, ValueError, TypeError) as exc:
            outcome["memory_candidates"]["rejected"].append(
                {"statement": _safe_echo(proposal.get("statement")), "reason": str(exc)}
            )
            continue

        if candidate.track_id:
            _owned_track(conn, profile, learner_id, candidate.track_id)

        try:
            _check_replacement_target(conn, profile, learner_id, candidate)
        except ValidationError as exc:
            outcome["memory_candidates"]["rejected"].append(
                {"statement": _safe_echo(proposal.get("statement")), "reason": str(exc)}
            )
            continue

        recorded_origin, recorded_state = _recorded_provenance(
            conn, profile, learner_id, candidate, outcome, proposal
        )
        expires_at = _candidate_expiry(candidate, config, outcome, proposal)
        if expires_at is _NOT_STORED:
            continue
        candidate_id = _new_id()
        timestamp = _now()
        conn.execute(
            "INSERT INTO memory_candidates"
            " (id, learner_id, profile_id, track_id, category, statement, evidence_summary,"
            "  origin, evidence_count, confidence, durability, confirmation_state,"
            "  recommended_action, replaces, consent_reference, consented_need,"
            "  expires_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate_id,
                learner_id,
                profile,
                candidate.track_id,
                candidate.category.value,
                candidate.statement,
                candidate.evidence_summary,
                recorded_origin.value,
                candidate.evidence_count,
                candidate.confidence.value,
                candidate.durability.value,
                recorded_state.value,
                candidate.recommended_action.value,
                candidate.replaces,
                candidate.consent_reference,
                candidate.consented_need,
                expires_at,
                timestamp,
                timestamp,
            ),
        )
        accepted = candidate.to_json()
        accepted["origin"] = recorded_origin.value
        accepted["confirmation_state"] = recorded_state.value
        accepted["candidate_id"] = candidate_id
        accepted["expires_at"] = expires_at
        outcome["memory_candidates"]["accepted"].append(accepted)


#: What a stored candidate may claim about how it was confirmed.
#:
#: ``learner_confirmed`` is deliberately unreachable. Nothing in a tool call
#: can establish that a learner confirmed anything: the flag, the origin, the
#: evidence summary and the consent statement are all written by the model in
#: the same request, and Hermes supplies no confirmation event to check them
#: against. A row that claimed otherwise would be a record asserting something
#: nobody verified, read months later as though somebody had.
#:
#: The proposal is still stored — it is useful, and the agent's own reading of
#: the conversation is real evidence — but labelled for what it is.
CONFIRMATION_NOT_VERIFIABLE = (
    "Recorded as a model proposal. This plugin cannot verify that a learner said, confirmed, "
    "corrected, or withdrew anything: the origin, the confirmation flag and the evidence all "
    "come from you, in this call, and Hermes supplies no confirmation event to check them "
    "against. The proposal is kept — labelled truthfully — so that a later reader is not told "
    "the learner agreed when nobody can show that they did."
)


def _recorded_provenance(
    conn: sqlite3.Connection,
    profile: str,
    learner_id: str,
    candidate: Any,
    outcome: dict[str, Any],
    proposal: dict[str, Any],
) -> tuple[Any, Any]:
    """The origin and confirmation state actually written down.

    Downgrading only the confirmation state left the row still *claiming*
    authority: ``origin = confirmed_long_term_goal`` beside
    ``confirmation_state = unconfirmed`` reads as "the learner confirmed this
    long-term goal, we just have not ticked the box". Both halves say
    something about the learner, so both are checked.

    One claim can be backed: ``confirmed_long_term_goal`` on a track this
    learner owns and confirmed. The track is itself created from a model
    flag, so it is not proof the learner spoke — but it *is* an owned,
    previously stored record rather than a value invented in this call, and it
    is the supporting record the goal is about. Everything else becomes
    ``model_proposed`` and ``unconfirmed``.
    """
    from .candidates import (
        AUTHORITATIVE_ORIGINS,
        AUTHORITATIVE_STATES,
        ConfirmationState,
        Origin,
    )

    origin = candidate.origin
    state = candidate.confirmation_state
    supported = _has_supporting_track(conn, profile, learner_id, candidate)

    recorded_origin = origin
    if origin in AUTHORITATIVE_ORIGINS and not (
        origin is Origin.CONFIRMED_LONG_TERM_GOAL and supported
    ):
        recorded_origin = Origin.MODEL_PROPOSED

    recorded_state = state
    if state in AUTHORITATIVE_STATES and not supported:
        recorded_state = ConfirmationState.UNCONFIRMED

    if recorded_origin is not origin or recorded_state is not state:
        outcome["memory_candidates"].setdefault("downgraded", []).append(
            {
                "statement": _safe_echo(proposal.get("statement")),
                "claimed": {"origin": origin.value, "confirmation_state": state.value},
                "recorded": {
                    "origin": recorded_origin.value,
                    "confirmation_state": recorded_state.value,
                },
                "reason": CONFIRMATION_NOT_VERIFIABLE,
            }
        )
    return recorded_origin, recorded_state


def _has_supporting_track(
    conn: sqlite3.Connection, profile: str, learner_id: str, candidate: Any
) -> bool:
    """True when the proposal names a track this learner owns and confirmed."""
    if not candidate.track_id:
        return False
    row = conn.execute(
        "SELECT status FROM tracks WHERE id = ? AND profile_id = ? AND learner_id = ?",
        (candidate.track_id, profile, learner_id),
    ).fetchone()
    return row is not None and str(row["status"]) == TrackStatus.ACTIVE.value


def _check_replacement_target(
    conn: sqlite3.Connection, profile: str, learner_id: str, candidate: Any
) -> None:
    """A replacement or removal must name a record that exists and is theirs.

    Without this, "replace what they said about X" is a free-text assertion
    about a record nobody looked for — and an agent acting on it would edit
    or delete something on the strength of a string the model composed.
    """
    from .candidates import Action

    if candidate.recommended_action not in (Action.REPLACE, Action.REMOVE):
        return
    if not candidate.replaces:
        raise ValidationError(
            f"a '{candidate.recommended_action.value}' candidate must name, in 'replaces', "
            "the existing proposal it changes"
        )

    target = _comparable(candidate.replaces)
    rows = conn.execute(
        "SELECT statement FROM memory_candidates WHERE profile_id = ? AND learner_id = ?",
        (profile, learner_id),
    ).fetchall()
    if not any(_comparable(str(row["statement"])) == target for row in rows):
        raise ValidationError(
            "'replaces' does not match any proposal stored for this learner. Name the "
            "existing statement exactly, or propose this as a new candidate instead of a "
            "replacement — a change to a record nobody can find is not a change."
        )


#: Sentinel meaning "do not write this row at all".
_NOT_STORED = object()

#: What ``durability`` actually does, now that it does something.
#:
#: ``session`` was the plain lie: a candidate labelled session-scoped was
#: inserted into SQLite and came back on every later call, forever. There is
#: no trustworthy per-session store here — a "session" this plugin could key
#: on would be another model-supplied string — so the honest implementation is
#: to return the proposal in the response and write nothing.
#:
#: ``short_term`` now expires, on the same window as temporary context, and
#: expired rows are swept before any read. ``durable`` is unchanged.
DURABILITY_NOT_STORED = (
    "Returned to you, and deliberately not stored. A 'session' candidate is scoped to this "
    "conversation, and this plugin has no session-scoped store to put it in — only durable "
    "SQLite. Keep it in the conversation, or propose it as 'short_term' if it should outlive "
    "the call."
)


def _candidate_expiry(
    candidate: Any, config: LearningStudioConfig, outcome: dict[str, Any], proposal: dict[str, Any]
) -> Any:
    """When this candidate stops being readable, or the not-stored sentinel."""
    from .candidates import Durability

    if candidate.durability is Durability.SESSION:
        entry = candidate.to_json()
        entry["stored"] = False
        entry["reason"] = DURABILITY_NOT_STORED
        outcome["memory_candidates"].setdefault("returned_not_stored", []).append(entry)
        return _NOT_STORED

    if candidate.durability is Durability.SHORT_TERM:
        return (datetime.now(UTC) + timedelta(hours=config.temporary_context_ttl_hours)).isoformat()

    return None


def purge_expired_candidates(conn: sqlite3.Connection, profile: str, limit: int = 500) -> int:
    """Delete short-term candidates whose window has closed.

    Profile-wide and bounded, exactly as :func:`purge_expired` is, and for the
    same reason: the learner whose proposal expired is the one who has not
    come back, so a per-learner sweep would never reach it.
    """
    rows = conn.execute(
        "SELECT id FROM memory_candidates"
        " WHERE profile_id = ? AND expires_at IS NOT NULL AND expires_at <= ?"
        " LIMIT ?",
        (profile, _now(), limit),
    ).fetchall()
    if not rows:
        return 0
    ids = [str(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"DELETE FROM memory_candidates WHERE id IN ({placeholders}) AND profile_id = ?",
        (*ids, profile),
    )
    return len(ids)


def _safe_echo(value: Any) -> str:
    """Echo a rejected statement back, truncated, so the agent can identify it."""
    text = str(value or "")
    return text[:80] + ("…" if len(text) > 80 else "")


# ── Experiences ───────────────────────────────────────────────────────────

#: Objectives a learner has finished with. Preparing new practice against one
#: would quietly reopen something they closed.
CLOSED_OBJECTIVE_STATUSES = (ObjectiveStatus.MET, ObjectiveStatus.RETIRED)


def prepare_experience(
    *,
    principal: Principal,
    manifest: Any,
    track_id: str | None = None,
    objective_id: str | None = None,
    config: LearningStudioConfig | None = None,
) -> dict[str, Any]:
    """Validate a learning experience and store it, or store nothing at all.

    The order is the point. The manifest is validated *before* the database is
    opened, so a malformed exercise never creates a row, never creates a
    learner, and never touches the filesystem. Ownership is then resolved from
    the trusted principal — never from the manifest, which has no field for it
    — and the whole write happens in one transaction.

    Nothing here writes context, objectives, or memory candidates. Preparing an
    exercise is not evidence about the learner, and an exercise that carries
    accessibility metadata must not thereby become a durable fact about them.
    """
    from .manifest import ManifestError, build_manifest

    config = config or load_config()

    try:
        validated = build_manifest(manifest)
    except ManifestError as exc:
        raise ValidationError(str(exc)) from exc

    profile = principal.profile
    storage.initialize(config)
    with storage.connect(config) as conn, storage.transaction(conn):
        learner_id = _get_or_create_learner(conn, principal)

        resolved_track = _resolve_experience_track(conn, profile, learner_id, track_id)
        resolved_objective = _resolve_experience_objective(
            conn, profile, learner_id, resolved_track, objective_id, validated
        )
        _authorise_accommodations(
            conn,
            profile=profile,
            learner_id=learner_id,
            track_id=resolved_track,
            accessibility=validated.accessibility,
            config=config,
        )

        experience_id = _insert_experience(
            conn,
            profile=profile,
            learner_id=learner_id,
            track_id=resolved_track,
            objective_id=resolved_objective,
            manifest=validated,
        )

    return {
        "ok": True,
        "learner": principal.describe(),
        "experience_id": experience_id,
        "track_id": resolved_track,
        "objective_id": resolved_objective,
        "stored": True,
        "experience": validated.learner_summary(),
        # Said on every response because the agent's next move depends on it:
        # something was *stored*, and nothing was started.
        "delivery": (
            "Stored only. No exercise has been launched, rendered, or opened, and there is "
            "no runtime yet. Present it in conversation and say that is what you are doing."
        ),
        "answers_withheld": (
            "Answer keys, rubrics, scoring rules, hints and feedback are stored server-side "
            "and are deliberately absent from this response."
        ),
        "hermes_memory_updated": False,
    }


def _resolve_experience_track(
    conn: sqlite3.Connection, profile: str, learner_id: str, track_id: Any
) -> str | None:
    """Confirm the caller owns the named track and that it still takes work."""
    if track_id is None:
        return None
    row = _writable_track(conn, profile, learner_id, str(track_id))
    return str(row["id"])


def _resolve_experience_objective(
    conn: sqlite3.Connection,
    profile: str,
    learner_id: str,
    track_id: str | None,
    objective_id: Any,
    manifest: Any,
) -> str | None:
    """An objective is addressable only through the track that owns it.

    And the experience must be *about* it. Verifying ownership while storing
    whatever objective text the caller supplied would let an experience claim
    to assess "add fractions, unaided, 4 of 5" while actually being a French
    translation drill — a record that reads as evidence of progress against
    something it never tested. The stored objective is authoritative, so the
    manifest has to agree with it exactly.
    """
    if objective_id is None:
        return None
    if track_id is None:
        raise ValidationError(
            "An objective belongs to a track, so objective_id needs the track_id it is on."
        )
    row = conn.execute(
        "SELECT * FROM objectives"
        " WHERE id = ? AND track_id = ? AND profile_id = ? AND learner_id = ?",
        (str(objective_id), track_id, profile, learner_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(NOT_FOUND_OBJECTIVE_MESSAGE)
    status = ObjectiveStatus(str(row["status"]))
    if status in CLOSED_OBJECTIVE_STATUSES:
        raise ValidationError(
            f"That objective is {status.value}. Ask the learner before practising against an "
            "objective they have finished with."
        )

    mismatched = [
        part
        for part in ("behavior", "condition", "standard")
        if _comparable(manifest.objective[part]) != _comparable(str(row[part]))
    ]
    if mismatched:
        raise ValidationError(
            "The objective in the manifest does not match the stored objective this "
            f"experience is attached to: {', '.join(mismatched)} differ(s). Send the stored "
            "objective's wording, or leave objective_id out for a one-off exercise."
        )
    return str(row["id"])


def _comparable(text: str) -> str:
    """Case- and whitespace-insensitive form, for objective agreement."""
    return " ".join(str(text).split()).casefold()


# ── Accessibility provenance ──────────────────────────────────────────────


def _authorise_accommodations(
    conn: sqlite3.Connection,
    *,
    profile: str,
    learner_id: str,
    track_id: str | None,
    accessibility: dict[str, Any],
    config: LearningStudioConfig,
) -> None:
    """Check the claimed source actually says what the manifest claims it says.

    A ``source`` string written by a model is a claim, not authorisation — the
    same reasoning that removed ``learner_key``. So each requested
    accommodation is looked up in the *named* source: a confirmed track's own
    context, the learner's explicitly stated session context, or the
    operator's configuration. A source that says nothing authorises nothing.

    Matching is exact on the canonical form of a recorded need, with no fuzzy,
    substring, or semantic step anywhere. That is the same rule PR 03 applies
    to accessibility consent, and for the same reason: a comparison loose
    enough to turn one need into another is a comparison that decides
    something about somebody's health on its own.
    """
    if not accessibility:
        return

    requested = list(accessibility.get("accommodations", ()))
    if not requested:
        return

    source = str(accessibility.get("source", ""))
    available = _recorded_needs(
        conn,
        profile=profile,
        learner_id=learner_id,
        track_id=track_id,
        source=source,
        config=config,
    )
    missing = sorted(set(requested) - available)
    if missing:
        raise ConsentError(
            f"This experience claims {', '.join(missing)} came from {source}, but nothing "
            f"recorded there says so. Accessibility metadata is only stored when the named "
            "source already holds that exact need: save it with "
            "learning_studio_save_context first, or leave accessibility off the manifest and "
            "honour the need in conversation instead."
        )


def _recorded_needs(
    conn: sqlite3.Connection,
    *,
    profile: str,
    learner_id: str,
    track_id: str | None,
    source: str,
    config: LearningStudioConfig,
) -> set[str]:
    """The accessibility needs one specific source records for this learner."""
    from .models import normalize_need

    def canonical(values: Any) -> set[str]:
        items = values if isinstance(values, list) else [values]
        out: set[str] = set()
        for item in items:
            try:
                out.add(normalize_need(item))
            except ValueError:  # pragma: no cover - stored values are validated
                continue
        return out

    if source == Provenance.PROFILE_CONFIG.value:
        return canonical(config.profile_context.get("accessibility_needs", []))

    return set()  # pragma: no cover - the enum admits nothing else


def _insert_experience(
    conn: sqlite3.Connection,
    *,
    profile: str,
    learner_id: str,
    track_id: str | None,
    objective_id: str | None,
    manifest: Any,
) -> str:
    """Write the experience and every component inside the caller's transaction.

    Each component's learner payload is re-checked immediately before it is
    written. That is not belt-and-braces about validation — it is the last
    point at which a hidden field could reach the learner-facing table, and a
    check *here* fails the whole transaction rather than storing a row that a
    later renderer would happily show.
    """
    from .manifest import dumps

    experience_id = _new_id()
    now = _now()
    conn.execute(
        "INSERT INTO experiences"
        " (id, learner_id, profile_id, track_id, objective_id, manifest_schema_version,"
        "  title, objective_behavior, objective_condition, objective_standard, instructions,"
        "  ui_locale, content_locale, expected_duration_minutes, difficulty, accessibility,"
        "  source_references, delivery, component_count, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            experience_id,
            learner_id,
            profile,
            track_id,
            objective_id,
            manifest.schema_version,
            manifest.title,
            manifest.objective["behavior"],
            manifest.objective["condition"],
            manifest.objective["standard"],
            manifest.instructions,
            manifest.ui_locale,
            manifest.content_locale,
            manifest.expected_duration_minutes,
            manifest.difficulty,
            dumps(manifest.accessibility),
            dumps(list(manifest.source_references)),
            dumps(manifest.delivery),
            manifest.component_count,
            now,
            now,
        ),
    )

    for position, component in enumerate(manifest.components, start=1):
        payload = component.learner_payload()
        _assert_payload_is_safe(payload, component)
        component_row_id = _new_id()
        conn.execute(
            "INSERT INTO experience_components"
            " (id, experience_id, learner_id, profile_id, position, component_key,"
            "  component_type, learner_payload, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                component_row_id,
                experience_id,
                learner_id,
                profile,
                position,
                component.id,
                component.type,
                dumps(payload),
                now,
            ),
        )
        hidden = component.hidden()
        if hidden:
            conn.execute(
                "INSERT INTO experience_component_evaluations"
                " (component_id, experience_id, learner_id, profile_id, evaluation, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (component_row_id, experience_id, learner_id, profile, dumps(hidden), now),
            )
    return experience_id


def _assert_payload_is_safe(payload: dict[str, Any], component: Any) -> None:
    """Refuse to write a learner payload carrying anything evaluator-only."""
    from .components import HIDDEN_KEYS, LEARNER_VISIBLE_KEYS

    # Hidden keys are checked first: they are also "unexpected fields", but a
    # leaked answer key and a misspelled property are not the same incident,
    # and the message should say which one happened.
    for hidden_key in HIDDEN_KEYS:
        if hidden_key in payload:
            raise ValidationError(
                f"component '{component.id}' would store evaluator-only data where the "
                "learner can read it"
            )
    stray = sorted(set(payload) - set(LEARNER_VISIBLE_KEYS))
    if stray:
        raise ValidationError(
            f"component '{component.id}' produced a learner payload with unexpected "
            f"field(s): {', '.join(stray)}"
        )


def get_experience(
    *,
    principal: Principal,
    experience_id: str,
    config: LearningStudioConfig | None = None,
) -> dict[str, Any]:
    """Read back a stored experience's learner-visible half.

    No tool exposes this yet — the delivery runtime arrives later. It exists
    now so that the storage guarantees this PR makes (ownership, ordering, and
    an evaluator-free projection) are testable against the code a runtime will
    actually call, rather than against a query written in a test.
    """
    config = config or load_config()
    profile = principal.profile

    storage.initialize(config)
    with storage.connect(config) as conn:
        learner_id = _find_learner(conn, principal)
        if learner_id is None:
            raise NotFoundError(NOT_FOUND_EXPERIENCE_MESSAGE)

        row = conn.execute(
            "SELECT * FROM experiences WHERE id = ? AND profile_id = ? AND learner_id = ?",
            (str(experience_id), profile, learner_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(NOT_FOUND_EXPERIENCE_MESSAGE)

        components = conn.execute(
            "SELECT position, component_key, component_type, learner_payload"
            " FROM experience_components"
            " WHERE experience_id = ? AND profile_id = ? AND learner_id = ?"
            " ORDER BY position",
            (str(experience_id), profile, learner_id),
        ).fetchall()

    return {
        "experience_id": str(row["id"]),
        "track_id": row["track_id"],
        "objective_id": row["objective_id"],
        "schema_version": int(row["manifest_schema_version"]),
        "title": str(row["title"]),
        "objective": {
            "behavior": str(row["objective_behavior"]),
            "condition": str(row["objective_condition"]),
            "standard": str(row["objective_standard"]),
        },
        "instructions": str(row["instructions"]),
        "ui_locale": str(row["ui_locale"]),
        "content_locale": row["content_locale"],
        "expected_duration_minutes": int(row["expected_duration_minutes"]),
        "difficulty": str(row["difficulty"]),
        "accessibility": json.loads(str(row["accessibility"])),
        "source_references": json.loads(str(row["source_references"])),
        "delivery": json.loads(str(row["delivery"])),
        "components": [
            {
                "position": int(component["position"]),
                "component_id": str(component["component_key"]),
                "type": str(component["component_type"]),
                "payload": json.loads(str(component["learner_payload"])),
            }
            for component in components
        ],
    }


# ── Input validation helpers ──────────────────────────────────────────────


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
    from .models import OBJECTIVE_TEXT_MAX, clean_text

    try:
        return clean_text(raw, label, OBJECTIVE_TEXT_MAX)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
