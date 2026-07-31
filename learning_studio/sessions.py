"""Expiring, opaque Mini App sessions.

A session is what turns "this Telegram account is authorised" into "this
request may read *this* exercise". It exists because the alternative — letting
each request name the experience it wants — makes every route an ownership
check away from being an enumeration oracle.

Four properties matter:

- **Opaque.** The token is random bytes. It encodes no user, no experience,
  and no expiry, so nothing about it can be decoded, forged, or extended by a
  client.
- **Scoped.** A session names a profile, a Telegram user, a learner, a track,
  and one experience. Every later request is answered inside that scope; a
  session for one experience cannot read another, and a token that leaks to a
  second Telegram account is refused, because the account is checked against
  the scope on every use.
- **Expiring.** Sessions carry a hard expiry and are swept on every access.
- **In memory only.** Nothing here is written to the database or the disk.
  Session tokens are bearer credentials, and this plugin creates no durable
  file that holds one. A restart ends every session, which is the correct
  behaviour for a process whose lifetime *is* the Mini App's availability.

The store keys sessions by the SHA-256 digest of the token, so the raw token
is never held in a structure that a heap dump, a log formatter, or a debugger
repr would casually print.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any

#: 32 bytes of ``secrets`` entropy, URL-safe. Long enough that guessing is not
#: a strategy and short enough to sit in a header.
_TOKEN_BYTES = 32

#: What a client is told when a session is missing, expired, or not theirs.
#: One message for all three: distinguishing them tells a caller whether a
#: token they hold is real, which is the one thing they must not learn.
SESSION_INVALID_MESSAGE = "This learning session is no longer valid. Reopen the exercise."


class SessionError(Exception):
    """No usable session for this request. ``reason`` is for logs only."""

    def __init__(self, reason: str) -> None:
        super().__init__(SESSION_INVALID_MESSAGE)
        self.reason = reason


@dataclass(frozen=True)
class SessionScope:
    """Everything a session is allowed to touch, fixed at creation.

    Nothing in this object is client-supplied: the profile comes from the
    host, the Telegram user from verified ``initData``, and the learner,
    track, and experience from an ownership-checked database read.
    """

    profile: str
    telegram_user_id: str
    learner_id: str
    experience_id: str
    track_id: str | None = None


@dataclass
class MiniAppSession:
    """One learner working through one experience, for a bounded time."""

    #: Non-secret handle for logs and metrics — derived from the token digest,
    #: so it identifies the session without being usable as one.
    ref: str
    scope: SessionScope
    created_at: float
    expires_at: float
    component_count: int
    #: ``auth_date`` of the verified payload that opened this session. Later
    #: requests must present a payload at least this fresh, so a session cannot
    #: be continued with an *older* captured launch than the one that created
    #: it. Zero when unset (test construction only).
    auth_date: int = 0
    #: Zero-based cursor into the experience's ordered components.
    position: int = 0
    #: ``component_id -> submitted response``. Held in memory for the life of
    #: the session; durable attempt storage belongs with the evaluation runtime,
    #: which is what will decide how long a learner's performance is kept.
    answers: dict[str, Any] = field(default_factory=dict)
    #: ``component_id -> the attempt recorded when the card was turned over``.
    #:
    #: This is what makes a reveal safe to grant. A learner may see the back of a
    #: flashcard only after committing an attempt, and the attempt is *frozen*
    #: here at that moment: the submission is later checked against this value, so
    #: reading the answer and then quietly improving the recall is refused rather
    #: than merely discouraged. It also makes a repeated reveal idempotent — a
    #: refresh returns the same card and keeps the first attempt.
    revealed: dict[str, str] = field(default_factory=dict)
    completed_at: float | None = None

    @property
    def completed(self) -> bool:
        return self.completed_at is not None

    def expired(self, now: float) -> bool:
        return now >= self.expires_at

    def attempt_before_reveal(self, component_id: str) -> str | None:
        """The frozen attempt for ``component_id``, or ``None`` if never revealed."""
        return self.revealed.get(component_id)

    def freeze_attempt(self, component_id: str, attempt: str) -> str:
        """Record the attempt that buys a reveal, and never overwrite it.

        Returning the stored value rather than the argument is the whole point:
        the second call gets the first call's attempt back, so a client that
        re-posts a different recall does not get to replace it.
        """
        return self.revealed.setdefault(component_id, attempt)


class SessionStore:
    """A bounded, in-memory map of token digest to session."""

    def __init__(self, *, ttl_seconds: int, max_sessions: int, clock) -> None:
        self._ttl = int(ttl_seconds)
        self._max = int(max_sessions)
        self._clock = clock
        self._sessions: dict[str, MiniAppSession] = {}

    def __len__(self) -> int:
        return len(self._sessions)

    def create(
        self, scope: SessionScope, *, component_count: int, auth_date: int = 0
    ) -> tuple[str, MiniAppSession]:
        """Mint a session and return ``(token, session)``.

        The token is returned exactly once, to the response that created it.
        The store keeps only its digest, so it cannot hand the token back —
        by design.
        """
        now = float(self._clock())
        self.purge_expired()
        self._enforce_capacity()

        token = secrets.token_urlsafe(_TOKEN_BYTES)
        digest = _digest(token)
        session = MiniAppSession(
            ref=digest[:12],
            scope=scope,
            created_at=now,
            expires_at=now + self._ttl,
            component_count=int(component_count),
            auth_date=int(auth_date),
        )
        self._sessions[digest] = session
        return token, session

    def resolve(self, token: object, *, profile: str, telegram_user_id: str) -> MiniAppSession:
        """Return the session this token names, for this exact caller.

        Raises :class:`SessionError` for absent, malformed, expired, and
        wrong-owner tokens alike — the caller cannot tell which happened.
        """
        if not isinstance(token, str) or not token.strip():
            raise SessionError("session_token_absent")

        now = float(self._clock())
        session = self._sessions.get(_digest(token))
        if session is None:
            raise SessionError("session_unknown")
        if session.expired(now):
            # Drop it here as well as in the sweep: an expired session must not
            # survive being asked for.
            self._sessions.pop(_digest(token), None)
            raise SessionError("session_expired")
        if session.scope.profile != profile:
            raise SessionError("session_wrong_profile")
        if session.scope.telegram_user_id != str(telegram_user_id):
            # A stolen or shared token is useless without the Telegram identity
            # it was minted for.
            raise SessionError("session_wrong_user")
        return session

    def drop(self, token: str) -> None:
        self._sessions.pop(_digest(token), None)

    def purge_expired(self) -> int:
        now = float(self._clock())
        stale = [digest for digest, s in self._sessions.items() if s.expired(now)]
        for digest in stale:
            del self._sessions[digest]
        return len(stale)

    def _enforce_capacity(self) -> None:
        """Keep memory bounded by retiring the sessions closest to expiry.

        Evicting rather than refusing is deliberate: refusing would let one
        caller who opens sessions in a loop lock every other learner out, and
        the evicted session's owner can reopen the exercise, which is a worse
        experience but not a lockout.
        """
        while len(self._sessions) >= self._max:
            oldest = min(self._sessions, key=lambda d: self._sessions[d].expires_at)
            del self._sessions[oldest]


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
