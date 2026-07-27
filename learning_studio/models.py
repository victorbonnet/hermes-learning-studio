"""The subject-neutral domain vocabulary.

Two things are load-bearing here and are asserted by tests:

1. **No subject is privileged.** The context fields describe *how* someone is
   learning, never *what*. There is no default subject, no default language,
   and no field that only makes sense for one discipline.
2. **Provenance is not decoration.** Every stored value records where it came
   from, and that origin decides which value wins in
   :mod:`learning_studio.context`. Losing provenance would make conflict
   resolution guesswork.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# ── Context fields ────────────────────────────────────────────────────────

#: Single-valued context fields.
SCALAR_FIELDS: tuple[str, ...] = (
    "track_name",
    "subject",
    "goal",
    "current_level",
    "target_level",
    "explanation_language",
    "content_language",
    "session_duration",
    "learning_horizon",
)

#: Multi-valued context fields, stored as a JSON array and returned as a list.
LIST_FIELDS: tuple[str, ...] = (
    "success_criteria",
    "prior_knowledge",
    "knowledge_gaps",
    "interests",
    "preferred_modalities",
    "assessment_preferences",
    "feedback_preferences",
    "accessibility_needs",
    "source_material",
    "constraints",
)

CONTEXT_FIELDS: tuple[str, ...] = SCALAR_FIELDS + LIST_FIELDS

#: Session-only unless the learner explicitly asks otherwise. Honoured fully
#: for the current session, never inferred, never durable by default. See
#: SKILL.md's consent rules — this is the code half of that contract.
SESSION_ONLY_FIELDS: frozenset[str] = frozenset({"accessibility_needs"})

MAX_LIST_ITEMS = 25


class Provenance(StrEnum):
    """Where a context value came from, ordered by how much it should be trusted.

    The order is the precedence order the resolver applies. It is declared
    once, here, so that the rule the skill documents and the rule the code
    applies cannot drift apart.
    """

    EXPLICIT_REQUEST = "explicit_request"
    EXPLICIT_CORRECTION = "explicit_correction"
    CONFIRMED_TRACK = "confirmed_track"
    PROFILE_CONFIG = "profile_config"
    CONFIRMED_PREFERENCE = "confirmed_preference"
    RECENT_EVIDENCE = "recent_evidence"
    DEFAULT = "default"
    #: An agent's guess. Ranks below every default: a guess must never
    #: present itself as fact, and must never win against a real value.
    UNCONFIRMED_INFERENCE = "unconfirmed_inference"


#: Lower wins. Built from declaration order so adding a provenance in the
#: right place is all it takes.
PRECEDENCE: dict[Provenance, int] = {p: i for i, p in enumerate(Provenance)}

#: Provenances that assert a confirmed fact about the learner. Everything
#: else is provisional and must be reported as such.
CONFIRMED_PROVENANCES: frozenset[Provenance] = frozenset(
    {
        Provenance.EXPLICIT_REQUEST,
        Provenance.EXPLICIT_CORRECTION,
        Provenance.CONFIRMED_TRACK,
        Provenance.CONFIRMED_PREFERENCE,
    }
)

#: Provenances a caller may write into a *durable* (track) context. Evidence
#: and inference are excluded on purpose: recent evidence may adapt temporary
#: context, but it must never silently rewrite what a learner confirmed.
DURABLE_WRITE_PROVENANCES: frozenset[Provenance] = frozenset(
    {
        Provenance.EXPLICIT_REQUEST,
        Provenance.EXPLICIT_CORRECTION,
        Provenance.CONFIRMED_TRACK,
        Provenance.CONFIRMED_PREFERENCE,
    }
)


class TrackStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    WITHDRAWN = "withdrawn"


class ObjectiveStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    MET = "met"
    RETIRED = "retired"


class ContextScope(StrEnum):
    TEMPORARY = "temporary"
    TRACK = "track"


class ChangeReason(StrEnum):
    """Why a context value changed — recorded on every revision."""

    EXPLICIT_REQUEST = "explicit_request"
    EXPLICIT_CORRECTION = "explicit_correction"
    CONFIRMED_TRACK = "confirmed_track"
    EVIDENCE = "evidence"


# ── Validation ────────────────────────────────────────────────────────────

MAX_VALUE_CHARS = 2000
MAX_NAME_CHARS = 120
MAX_LEARNER_KEY_CHARS = 256
#: Objectives are one sentence each — behaviour, condition, standard.
OBJECTIVE_TEXT_MAX = 500

#: Learner keys are opaque identifiers supplied by the caller. Printable,
#: bounded, no control characters — enough to accept a platform user id or a
#: UUID while rejecting a pasted transcript or a newline-injection attempt.
_LEARNER_KEY_RE = re.compile(r"^[\w.:@+\-]{1,256}$")

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def validate_learner_key(raw: Any) -> str:
    """Validate a caller-supplied learner identity.

    Rejects ambiguity rather than normalising it: a blank or whitespace-only
    key would otherwise collapse several learners into one record, which is
    the isolation failure this whole module exists to prevent.
    """
    if not isinstance(raw, str):
        raise ValueError("learner_key must be a string")
    key = raw.strip()
    if not key:
        raise ValueError("learner_key must not be empty")
    if len(key) > MAX_LEARNER_KEY_CHARS:
        raise ValueError(f"learner_key must be at most {MAX_LEARNER_KEY_CHARS} characters")
    if not _LEARNER_KEY_RE.match(key):
        raise ValueError(
            "learner_key must be an opaque stable identifier "
            "(letters, digits, and . : @ + _ - only)"
        )
    return key


def clean_text(raw: Any, label: str, max_chars: int) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"{label} must be a string")
    if _CONTROL_CHARS_RE.search(raw):
        raise ValueError(f"{label} must not contain control characters")
    text = raw.strip()
    if not text:
        raise ValueError(f"{label} must not be empty")
    if len(text) > max_chars:
        raise ValueError(f"{label} must be at most {max_chars} characters")
    return text


def validate_field_value(field: str, raw: Any, max_chars: int = MAX_VALUE_CHARS) -> Any:
    """Validate one context value, returning its normalised form.

    List fields normalise to a list of non-empty strings; scalar fields to a
    single trimmed string.
    """
    if field not in CONTEXT_FIELDS:
        raise ValueError(f"unknown context field: {field}")

    if field in LIST_FIELDS:
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            raise ValueError(f"{field} must be a list of strings")
        if len(raw) > MAX_LIST_ITEMS:
            raise ValueError(f"{field} must have at most {MAX_LIST_ITEMS} entries")
        items = [clean_text(item, f"{field} entry", max_chars) for item in raw]
        if not items:
            raise ValueError(f"{field} must not be empty")
        return items

    return clean_text(raw, field, max_chars)


def validate_context_payload(payload: Any, max_chars: int = MAX_VALUE_CHARS) -> dict[str, Any]:
    """Validate a whole ``{field: value}`` mapping of context fields."""
    if not isinstance(payload, dict):
        raise ValueError("context must be a mapping of context fields to values")
    return {field: validate_field_value(field, raw, max_chars) for field, raw in payload.items()}


def validate_track_name(raw: Any) -> str:
    return clean_text(raw, "track name", MAX_NAME_CHARS)


def encode_value(value: Any) -> str:
    """Serialise a validated context value for storage."""
    return json.dumps(value, ensure_ascii=False) if isinstance(value, list) else str(value)


def decode_value(field: str, stored: str) -> Any:
    """Inverse of :func:`encode_value`.

    A list field whose stored text is not valid JSON is returned as a
    single-item list rather than raising: a readable degraded value beats an
    unreadable context, and the only way to get here is manual DB editing.
    """
    if field not in LIST_FIELDS:
        return stored
    try:
        decoded = json.loads(stored)
    except json.JSONDecodeError:
        return [stored]
    return decoded if isinstance(decoded, list) else [stored]


@dataclass(frozen=True)
class ResolvedValue:
    """One resolved context field, with the reasoning that selected it."""

    field: str
    value: Any
    provenance: Provenance
    #: True only for provenances that assert a confirmed fact. An agent must
    #: not present an unconfirmed value to the learner as something they said.
    confirmed: bool
    #: ``current_request`` | ``track`` | ``temporary`` | ``profile_config`` | ``default``
    source: str
    recorded_at: str | None = None
    #: Lower-precedence candidates for the same field, best first. This is
    #: what lets a caller explain *why* a value won.
    superseded: tuple[dict[str, Any], ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "provenance": self.provenance.value,
            "confirmed": self.confirmed,
            "source": self.source,
            "recorded_at": self.recorded_at,
            "superseded": list(self.superseded),
        }
