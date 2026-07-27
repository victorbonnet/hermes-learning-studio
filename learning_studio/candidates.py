"""Memory candidates: proposals, not writes.

A memory candidate is this plugin saying to the agent "this *might* be worth
remembering about the learner". The agent decides. This plugin has no path to
Hermes memory — no import, no dispatch, no subprocess — and the tests in
``tests/test_no_memory_access.py`` exist to keep it that way.

What this module actually does is say **no** a lot, in two directions:

- **Origin.** A candidate has to come from something durable: a stated
  preference, a confirmed goal, a correction, a withdrawal, or evidence
  repeated often enough to be worth asking about. One wrong answer, one slow
  reply, or one moment of frustration is not evidence of anything lasting.
- **Content.** Even a well-founded candidate must not carry raw answers,
  attempts, transcripts, session identifiers, tokens, or an inference about
  someone's disabilities or diagnoses. Those are either the wrong store or
  nobody's business.

The bar is deliberately high. A wrong permanent record about a person costs
far more than a missed one, and the learner can always be asked again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .models import MAX_VALUE_CHARS, clean_text


class CandidateRejected(ValueError):
    """A proposed candidate violated a generation or content boundary."""


class Category(StrEnum):
    """What kind of durable fact is being proposed."""

    DURABLE_PREFERENCE = "durable_preference"
    LONG_TERM_GOAL = "long_term_goal"
    TARGET_LEVEL = "target_level"
    #: Session-only unless the learner explicitly asks otherwise.
    ACCESSIBILITY = "accessibility"
    PRIVACY_PREFERENCE = "privacy_preference"


class Origin(StrEnum):
    """Where the proposal came from. Only these five may produce a candidate."""

    EXPLICIT_DURABLE_PREFERENCE = "explicit_durable_preference"
    CONFIRMED_LONG_TERM_GOAL = "confirmed_long_term_goal"
    REPEATED_EVIDENCE = "repeated_evidence"
    EXPLICIT_CORRECTION = "explicit_correction"
    EXPLICIT_WITHDRAWAL = "explicit_withdrawal"


#: Origins a caller might reach for that must never yield a candidate. Named
#: explicitly so the refusal is a documented rule with a reason attached,
#: rather than an unexplained validation failure.
FORBIDDEN_ORIGINS: dict[str, str] = {
    "single_error": "one wrong answer is not a durable fact about a learner",
    "single_slow_response": (
        "response latency varies with reading speed, typing, and mood; it is not "
        "evidence of weak mastery unless the objective asked for speed"
    ),
    "single_inference": "an unconfirmed inference must be confirmed before it is durable",
    "temporary_frustration": "a bad session is a mood, not a preference",
    "raw_score": "scores belong in Studio storage, never in durable memory",
    "raw_attempts": "attempt history belongs in Studio storage, never in durable memory",
    "session_state": "short-lived session state is not durable by definition",
}


class Action(StrEnum):
    ADD = "add"
    REPLACE = "replace"
    REMOVE = "remove"
    NO_ACTION = "no_action"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Durability(StrEnum):
    SESSION = "session"
    SHORT_TERM = "short_term"
    DURABLE = "durable"


class ConfirmationState(StrEnum):
    UNCONFIRMED = "unconfirmed"
    LEARNER_CONFIRMED = "learner_confirmed"
    LEARNER_DECLINED = "learner_declined"


# ── Content boundaries ────────────────────────────────────────────────────

#: Patterns for material that must never reach a durable proposal. Each entry
#: is (name, pattern, why) so a rejection can explain itself to the agent
#: instead of just failing.
_FORBIDDEN_CONTENT: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "credential",
        re.compile(
            r"\b(?:api[_\s-]?key|secret|password|passwd|bearer|access[_\s-]?token|"
            r"auth[_\s-]?token|cookie|credential)\b",
            re.IGNORECASE,
        ),
        "credentials and secrets never belong in memory",
    ),
    (
        "token_literal",
        # Long opaque blobs: JWTs, hex keys, base64 secrets.
        re.compile(r"\b(?:eyJ[\w-]{10,}|[A-Fa-f0-9]{32,}|[A-Za-z0-9+/]{40,}={0,2})\b"),
        "this looks like a token or key literal",
    ),
    (
        "telegram_init_data",
        re.compile(r"\b(?:init[_\s-]?data|hash=|query_id=|auth_date=)", re.IGNORECASE),
        "Telegram initData is an authentication artefact, not a learning fact",
    ),
    (
        "session_id",
        re.compile(r"\b(?:session[_\s-]?id|task[_\s-]?id|conversation[_\s-]?id)\b", re.IGNORECASE),
        "session identifiers are short-lived and identify a conversation, not a learner",
    ),
    (
        "tunnel_url",
        re.compile(r"https?://[\w.-]*(?:trycloudflare\.com|ngrok\.[\w.]+|loca\.lt)", re.IGNORECASE),
        "tunnel URLs are ephemeral infrastructure",
    ),
    (
        "raw_answer",
        re.compile(
            r"\b(?:answered|responded|typed|submitted|guessed)\b[^.]{0,40}\b(?:with|that|:)\b|"
            r"\b(?:their|the)\s+(?:raw\s+)?(?:answer|attempt|response)\s+was\b",
            re.IGNORECASE,
        ),
        "raw answers belong in Studio storage, never in durable memory",
    ),
    (
        "raw_score",
        re.compile(
            r"\b(?:scored|got)\s+\d+\s*(?:/|out of|%)|"
            r"\b\d+\s*(?:/\s*\d+|%)\s+(?:on|correct)\b",
            re.IGNORECASE,
        ),
        "scores belong in Studio storage, never in durable memory",
    ),
    (
        "transcript",
        re.compile(r"\b(?:transcript|full conversation|chat log|verbatim)\b", re.IGNORECASE),
        "transcripts are never durable memory material",
    ),
    (
        "inferred_sensitive_trait",
        re.compile(
            r"\b(?:seems|appears|probably|likely|might|may)\s+(?:to\s+)?(?:be|have)\b"
            r"[^.]{0,60}\b(?:dyslexi\w*|dyspraxi\w*|adhd|autis\w*|asperger\w*|"
            r"disab\w*|disorder|diagnos\w*|depress\w*|anxiet\w*|anxious|impair\w*)\b",
            re.IGNORECASE,
        ),
        "a disability or diagnosis must never be inferred — only the learner may state it",
    ),
)

#: Diagnosis and disability language, wherever it appears. Permitted only in
#: an ``accessibility`` candidate the learner explicitly asked to be
#: remembered — never inferred, never in another category.
_SENSITIVE_TRAIT_RE = re.compile(
    r"\b(?:dyslexi\w*|dyspraxi\w*|adhd|autis\w*|asperger\w*|disabilit\w*|disabled|"
    r"disorder|diagnos\w*|medication|therapy|depress\w*|anxiet\w*|ptsd)\b",
    re.IGNORECASE,
)


def _scan(text: str, label: str) -> None:
    for name, pattern, why in _FORBIDDEN_CONTENT:
        if pattern.search(text):
            raise CandidateRejected(f"{label} rejected ({name}): {why}")


@dataclass(frozen=True)
class MemoryCandidate:
    """A validated proposal. Construct it through :func:`propose`."""

    category: Category
    statement: str
    evidence_summary: str
    confidence: Confidence
    durability: Durability
    confirmation_state: ConfirmationState
    recommended_action: Action
    origin: Origin
    replaces: str | None = None
    track_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "statement": self.statement,
            "evidence_summary": self.evidence_summary,
            "confidence": self.confidence.value,
            "durability": self.durability.value,
            "confirmation_state": self.confirmation_state.value,
            "recommended_action": self.recommended_action.value,
            "origin": self.origin.value,
            "replaces": self.replaces,
            "track_id": self.track_id,
        }


def _enum(enum_cls: type[StrEnum], raw: Any, label: str) -> Any:
    try:
        return enum_cls(raw)
    except ValueError:
        allowed = ", ".join(member.value for member in enum_cls)
        raise CandidateRejected(f"{label} must be one of: {allowed}") from None


def propose(
    *,
    category: Any,
    statement: Any,
    evidence_summary: Any,
    origin: Any,
    recommended_action: Any = Action.ADD.value,
    confidence: Any = Confidence.MEDIUM.value,
    durability: Any = Durability.DURABLE.value,
    confirmation_state: Any = ConfirmationState.UNCONFIRMED.value,
    replaces: Any = None,
    track_id: str | None = None,
    evidence_count: int = 1,
    min_evidence: int = 3,
    learner_permitted_accessibility: bool = False,
) -> MemoryCandidate:
    """Validate a proposed candidate, or refuse it with a reason.

    ``evidence_count`` matters only for :attr:`Origin.REPEATED_EVIDENCE`,
    where it is checked against ``min_evidence`` — the point of that origin is
    that a pattern has recurred often enough to be worth *asking* the learner
    about, and a single observation has not.
    """
    if isinstance(origin, str) and origin in FORBIDDEN_ORIGINS:
        raise CandidateRejected(f"no memory candidate from '{origin}': {FORBIDDEN_ORIGINS[origin]}")
    origin = _enum(Origin, origin, "origin")
    category = _enum(Category, category, "category")
    recommended_action = _enum(Action, recommended_action, "recommended_action")
    confidence = _enum(Confidence, confidence, "confidence")
    durability = _enum(Durability, durability, "durability")
    confirmation_state = _enum(ConfirmationState, confirmation_state, "confirmation_state")

    try:
        statement = clean_text(statement, "statement", MAX_VALUE_CHARS)
        evidence_summary = clean_text(evidence_summary, "evidence_summary", MAX_VALUE_CHARS)
    except ValueError as exc:
        raise CandidateRejected(str(exc)) from exc

    if replaces is not None:
        try:
            replaces = clean_text(replaces, "replaces", MAX_VALUE_CHARS)
        except ValueError as exc:
            raise CandidateRejected(str(exc)) from exc

    if recommended_action is Action.REPLACE and not replaces:
        raise CandidateRejected(
            "recommended_action 'replace' requires 'replaces' naming the entry it supersedes — "
            "an unreplaced contradiction is worse than no entry"
        )

    if origin is Origin.REPEATED_EVIDENCE and evidence_count < min_evidence:
        raise CandidateRejected(
            f"repeated evidence needs at least {min_evidence} independent observations, "
            f"got {evidence_count}; keep it in temporary context until the pattern holds"
        )

    for label, text in (("statement", statement), ("evidence_summary", evidence_summary)):
        _scan(text, label)

    _check_sensitive(category, statement, evidence_summary, learner_permitted_accessibility)

    return MemoryCandidate(
        category=category,
        statement=statement,
        evidence_summary=evidence_summary,
        confidence=confidence,
        durability=durability,
        confirmation_state=confirmation_state,
        recommended_action=recommended_action,
        origin=origin,
        replaces=replaces,
        track_id=track_id,
    )


def _check_sensitive(
    category: Category, statement: str, evidence: str, learner_permitted: bool
) -> None:
    """Gate disability, diagnosis, and accessibility material on real consent.

    Accessibility needs are honoured fully for the session either way; this
    only governs whether they may become a durable proposal. A dedicated
    profile is not consent, and neither is the agent's confidence.
    """
    combined = f"{statement}\n{evidence}"
    mentions_sensitive = bool(_SENSITIVE_TRAIT_RE.search(combined))

    if category is Category.ACCESSIBILITY:
        if not learner_permitted:
            raise CandidateRejected(
                "accessibility needs stay session-only unless the learner explicitly asked "
                "for them to be remembered; honour the need now and do not persist it"
            )
        return

    if mentions_sensitive:
        raise CandidateRejected(
            "sensitive health, disability, or diagnosis material cannot be proposed under "
            f"category '{category.value}'; only an accessibility candidate the learner "
            "explicitly asked to be remembered may carry it"
        )
