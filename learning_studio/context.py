"""Context resolution: deciding which value for a field actually applies.

A learner's context arrives from several directions at once — what they just
said, what they corrected last week, what their confirmed track records, what
the profile is configured for, what recent exercises suggest. These disagree
routinely, and the disagreement is the interesting part.

The rule, in order of decreasing authority:

    current explicit request
      > explicit correction
      > active confirmed track
      > profile configuration
      > confirmed durable preferences
      > recent evidence
      > safe defaults
      > unconfirmed inference

Two consequences are worth stating plainly, because they are the ones that
make the difference between a helpful assistant and an infuriating one:

- **What the learner says now wins.** Saved context is never a reason to
  override someone who has just told you something different, however
  confidently it was stored.
- **Defaults never overwrite anything.** They fill gaps, and that is all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import (
    CONFIRMED_PROVENANCES,
    CONTEXT_FIELDS,
    PRECEDENCE,
    Provenance,
    ResolvedValue,
)

#: How a stored value's authority differs from the same value in flight.
#:
#: ``explicit_request`` at the top of the precedence order means *the request
#: being made right now*. Once saved it is last session's remark, and it must
#: not outrank a correction the learner made afterwards, nor a track they have
#: since confirmed. Saved, it carries the weight of a stated preference — above
#: anything merely inferred, below anything durable.
STORED_DEMOTION: dict[Provenance, Provenance] = {
    Provenance.EXPLICIT_REQUEST: Provenance.CONFIRMED_PREFERENCE,
}


@dataclass(frozen=True)
class Candidate:
    """One possible value for a field, before precedence is applied."""

    field: str
    value: Any
    provenance: Provenance
    source: str
    recorded_at: str | None = None
    #: True only for the request being handled right now. Distinguishes a
    #: live statement from a stored one that was explicit when it was made.
    is_current: bool = False

    @property
    def confirmed(self) -> bool:
        return self.provenance in CONFIRMED_PROVENANCES

    @property
    def effective_provenance(self) -> Provenance:
        """The provenance that decides precedence, after stored demotion."""
        if self.is_current:
            return self.provenance
        return STORED_DEMOTION.get(self.provenance, self.provenance)

    def _sort_key(self) -> tuple[Any, ...]:
        # Effective precedence first; a live statement beats a stored one at
        # equal precedence; then most recent; then source name so that two
        # otherwise identical candidates still order deterministically.
        return (
            PRECEDENCE[self.effective_provenance],
            0 if self.is_current else 1,
            _descending(self.recorded_at),
            self.source,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "provenance": self.provenance.value,
            "source": self.source,
            "confirmed": self.confirmed,
            "recorded_at": self.recorded_at,
        }


class _Descending:
    """Sorts newer timestamps first; a missing timestamp sorts last."""

    __slots__ = ("value",)

    def __init__(self, value: str | None) -> None:
        self.value = value

    def __lt__(self, other: _Descending) -> bool:
        if self.value is None:
            return False
        if other.value is None:
            return True
        return self.value > other.value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Descending) and self.value == other.value


def _descending(value: str | None) -> _Descending:
    return _Descending(value)


def resolve(candidates: list[Candidate]) -> dict[str, ResolvedValue]:
    """Resolve competing candidates into one value per field.

    Deterministic: the same candidate set always produces the same answer,
    including which losing candidates are reported and in what order.
    """
    by_field: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        if candidate.field not in CONTEXT_FIELDS:
            continue
        by_field.setdefault(candidate.field, []).append(candidate)

    resolved: dict[str, ResolvedValue] = {}
    for field, field_candidates in by_field.items():
        ordered = sorted(field_candidates, key=Candidate._sort_key)
        winner = ordered[0]
        resolved[field] = ResolvedValue(
            field=field,
            value=winner.value,
            provenance=winner.provenance,
            confirmed=winner.confirmed,
            source=winner.source,
            recorded_at=winner.recorded_at,
            superseded=tuple(loser.to_json() for loser in ordered[1:]),
        )
    return resolved


def candidates_from_request(request: dict[str, Any] | None) -> list[Candidate]:
    """Candidates for what the learner is asking for right now."""
    if not request:
        return []
    return [
        Candidate(
            field=field,
            value=value,
            provenance=Provenance.EXPLICIT_REQUEST,
            source="current_request",
            is_current=True,
        )
        for field, value in request.items()
        if field in CONTEXT_FIELDS
    ]


def candidates_from_config(
    profile_context: dict[str, Any], defaults: dict[str, Any]
) -> list[Candidate]:
    """Candidates contributed by ``config.yaml``.

    Profile configuration is an operator decision and outranks stored
    preferences; defaults are the floor and outrank nothing.
    """
    candidates = [
        Candidate(
            field=field,
            value=value,
            provenance=Provenance.PROFILE_CONFIG,
            source="profile_config",
        )
        for field, value in (profile_context or {}).items()
        if field in CONTEXT_FIELDS
    ]
    candidates += [
        Candidate(field=field, value=value, provenance=Provenance.DEFAULT, source="default")
        for field, value in (defaults or {}).items()
        if field in CONTEXT_FIELDS
    ]
    return candidates
