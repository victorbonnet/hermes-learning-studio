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
import threading
from copy import deepcopy
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


class SessionTransitionError(Exception):
    """This session will not make that move.

    ``reason`` is a fixed internal token — ``already_complete``,
    ``no_component``, ``component_mismatch``, ``type_not_revealable``,
    ``reveal_required``, ``recall_changed_after_reveal``, ``not_completed``,
    ``session_retired`` — for
    logs and for the API's own translation table. It is never a message for a
    client: what a caller is told depends on which route was asked, and only the
    web layer knows that.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SessionStep:
    """One place in the ordered walk through an experience.

    Deliberately tiny, and built from the *learner-facing* component list. A
    step names the component and says whether its card has to be turned over
    before it may be answered; it holds no answer, rubric, hint, or feedback, so
    the ordered plan a session carries discloses nothing the learner was not
    already served.
    """

    component_id: str
    reveal_required: bool = False


@dataclass(frozen=True)
class RevealOutcome:
    """What a granted reveal disclosed, and the attempt that bought it."""

    # Both values are deliberately omitted from repr: one is evaluator-only
    # content and the other is the learner's raw recall.
    back: str = field(repr=False)
    attempt: str = field(repr=False)


@dataclass(frozen=True)
class SessionCompletion:
    """A finished session, copied, for whatever scores it.

    Handed to the scorer instead of the session itself, so that scoring cannot
    advance, re-answer, or re-time the thing it is scoring. The responses are a
    deep copy and both timestamps are this session's own.

    ``created_at`` is therefore when *this* session opened, not when the learner
    first started: a session that resumed another deliberately keeps its own
    creation time, so after a reload the durable attempt reports the replacement's
    start. That is what the previous implementation recorded too; preserving the
    original learner start would be a separate attempt-timing contract change.
    """

    # Raw learner responses must not appear if a callback logs the snapshot.
    responses: dict[str, Any] = field(repr=False)
    created_at: float
    completed_at: float


@dataclass
class MiniAppSession:
    """One learner working through one experience, for a bounded time.

    The lifecycle lives here: which component is current, what has been
    answered, which cards were turned over and with what attempt, when the
    session finished, and the one durable result it produced. Callers ask for
    transitions — :meth:`record_answer`, :meth:`reveal`, :meth:`score_once`,
    :meth:`resume_from` — and read snapshots. None of them can assign the state
    behind those transitions, which is what keeps "may this move happen?" a
    question with one answer rather than one per caller.
    """

    #: Non-secret handle for logs and metrics — derived from the token digest,
    #: so it identifies the session without being usable as one.
    ref: str
    scope: SessionScope
    created_at: float
    expires_at: float
    #: The ordered walk, fixed at creation. Its length is the component count
    #: and the position indexes into it, so there is no second number that
    #: could disagree with the plan.
    _steps: tuple[SessionStep, ...] = field(repr=False)
    _clock: Any = field(repr=False, compare=False)
    #: ``auth_date`` of the verified payload that opened this session. Later
    #: requests must present a payload at least this fresh, so a session cannot
    #: be continued with an *older* captured launch than the one that created
    #: it. Zero when unset (test construction only).
    auth_date: int = 0
    #: Zero-based cursor into :attr:`steps`.
    _position: int = field(default=0, init=False)
    #: ``component_id -> submitted response``. Held in memory for the life of
    #: the session; durable attempt storage belongs with the evaluation runtime,
    #: which is what will decide how long a learner's performance is kept.
    #: Kept out of ``repr`` so no learner response can reach a log through one.
    _responses: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    #: ``component_id -> the attempt recorded when the card was turned over``.
    #:
    #: This is what makes a reveal safe to grant. A learner may see the back of a
    #: flashcard only after committing an attempt, and the attempt is *frozen*
    #: here at that moment: the submission is later checked against this value, so
    #: reading the answer and then quietly improving the recall is refused rather
    #: than merely discouraged. It also makes a repeated reveal idempotent — a
    #: refresh returns the same card and keeps the first attempt.
    _revealed: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _completed_at: float | None = field(default=None, init=False)
    #: The durable attempt this session produced, computed once and cached
    #: here — never recomputed. A second request for the completion screen
    #: (a refresh, a backgrounded webview resuming) must see the exact same
    #: scored result rather than triggering ``record_attempt`` again, which
    #: would otherwise write a second row for one finished session.
    _attempt_result: dict[str, Any] | None = field(default=None, init=False, repr=False)
    #: One lock owns every lifecycle transition and snapshot. The runtime is a
    #: single worker today, but reload and control-plane calls can arrive from
    #: different threads; check-then-commit must still be one operation.
    _lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False, compare=False
    )
    #: Capacity eviction invalidates the bearer token but is not a hard learner
    #: expiry. The grant may copy this state into a replacement while its own
    #: admission window is still live.
    _capacity_evicted: bool = field(default=False, init=False, repr=False)
    _retired: bool = field(default=False, init=False, repr=False)
    #: Token retirement may zero ``expires_at`` immediately, but it must never
    #: erase the original hard deadline used to decide whether state may resume.
    _hard_expires_at: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        component_ids = [step.component_id for step in self._steps]
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("session plan has a duplicate component_id")
        self._hard_expires_at = self.expires_at

    # -- what may be read ------------------------------------------------

    @property
    def steps(self) -> tuple[SessionStep, ...]:
        """The immutable ordered component plan."""
        return self._steps

    @property
    def component_count(self) -> int:
        return len(self._steps)

    @property
    def position(self) -> int:
        with self._lock:
            return self._position

    @property
    def completed_at(self) -> float | None:
        with self._lock:
            return self._completed_at

    @property
    def completed(self) -> bool:
        with self._lock:
            return self._completed_at is not None

    @property
    def scored(self) -> bool:
        """Whether this session already produced its one durable attempt."""
        with self._lock:
            return self._attempt_result is not None

    @property
    def responses(self) -> dict[str, Any]:
        """``component_id -> response``, copied. Mutating it changes nothing."""
        with self._lock:
            return deepcopy(self._responses)

    @property
    def answered_component_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._responses))

    @property
    def revealed_attempts(self) -> dict[str, str]:
        """The frozen attempts, copied. Reading them cannot rewrite one."""
        with self._lock:
            return dict(self._revealed)

    @property
    def progress(self) -> dict[str, Any]:
        """How far through the exercise this session is, as a fresh snapshot.

        A plain mapping rather than an object because it is what the API sends
        to a client verbatim, and a new one every time so that handing it out is
        not handing out the state itself.
        """
        with self._lock:
            return {
                "position": self._position,
                "component_count": len(self._steps),
                "answered": len(self._responses),
                "completed": self._completed_at is not None,
            }

    def expired(self, now: float) -> bool:
        with self._lock:
            return self._retired or now >= self.expires_at

    @property
    def resumable_after_capacity_eviction(self) -> bool:
        with self._lock:
            return self._capacity_evicted and float(self._clock()) < self._hard_expires_at

    def retire_for_capacity(self) -> None:
        """Invalidate this token while retaining grant-resumable state."""
        with self._lock:
            self._capacity_evicted = True
            self._retired = True
            self.expires_at = 0.0

    def retire(self) -> None:
        """Invalidate this token without making its state resumable."""
        with self._lock:
            self._retired = True
            self.expires_at = 0.0

    # -- transitions -----------------------------------------------------

    def require_current_step(self, component_id: object) -> SessionStep:
        """The step a request may act on, or refuse it. Nothing is mutated.

        The one place the order rule is written down. A caller that wants the
        current component in order to *validate* something against it — the API
        checks a submitted response against the component it was served — asks
        here rather than comparing identifiers itself, so there is no second
        version of "is this the current question?" to drift.
        """
        with self._lock:
            return self._require_current_step(component_id)

    def _require_current_step(self, component_id: object) -> SessionStep:
        self._ensure_active()
        if self._completed_at is not None:
            raise SessionTransitionError("already_complete")
        if not 0 <= self._position < len(self._steps):
            raise SessionTransitionError("no_component")
        step = self._steps[self._position]
        if not isinstance(component_id, str) or component_id != step.component_id:
            # A stale client, or one naming a card it has not reached. Neither
            # may move the session, and neither is told which it was.
            raise SessionTransitionError("component_mismatch")
        return step

    def _ensure_active(self) -> None:
        if self._retired or float(self._clock()) >= self._hard_expires_at:
            self._retired = True
            self.expires_at = 0.0
            raise SessionTransitionError("session_retired")

    def record_answer(self, component_id: object, response: Any, *, now: float) -> dict[str, Any]:
        """Record the response to the current component and advance one step.

        Refuses — without touching anything — a session that is already
        finished, one with no component to answer, and any component that is not
        the current one. The completion timestamp is stamped here, on the
        transition that reaches the end, so "finished" and "when" cannot
        disagree.
        """
        with self._lock:
            step = self._require_current_step(component_id)
            if step.reveal_required:
                self._require_frozen_recall(step.component_id, response)
            self._responses[step.component_id] = deepcopy(response)
            self._position += 1
            if self._position >= len(self._steps):
                self._completed_at = float(now)
            return {
                "position": self._position,
                "component_count": len(self._steps),
                "answered": len(self._responses),
                "completed": self._completed_at is not None,
            }

    def _require_frozen_recall(self, component_id: str, response: Any) -> None:
        """A card whose answer can be shown may only be submitted after it was.

        Two rules, and the second is the one that matters:

        1. The card must have been turned over. Self-rating a recall you never
           committed is not retrieval practice, and the component contract this
           plugin publishes says the reveal is part of the interaction rather
           than an optional extra.
        2. The submitted recall must be the recall that bought the reveal.
           Without this, the sequence "commit anything, read the answer, replace
           the recall with the answer, rate yourself Easy" is available to any
           client — and it would be invisible in the stored attempt.

        Enforced here rather than in the frontend, or in the route, because the
        frontend is a convenience and a route is one caller: anybody can post
        to the API directly, and the session is what knows what was frozen.
        """
        frozen = self._revealed.get(component_id)
        if frozen is None:
            raise SessionTransitionError("reveal_required")
        submitted = response.get("text") if isinstance(response, dict) else None
        if submitted != frozen:
            raise SessionTransitionError("recall_changed_after_reveal")

    def reveal(self, component_id: object, attempt: str, disclose) -> RevealOutcome:
        """Turn the current card over, having been paid for with ``attempt``.

        ``disclose`` is injected — it is the one authorised read of the hidden
        half, and it belongs to the service, not here — and it is called
        **before** anything is committed. Freezing first meant a card that could
        not be turned over still recorded the attempt that bought the reveal,
        and the learner was then held to a recall they never got an answer for;
        now a failed disclosure changes nothing and the next attempt is the one
        that counts.

        Whether the card *has* a back is read from the immutable step, so an
        unrevealable type is refused without asking the service at all.
        Repeating a granted reveal returns the first frozen attempt, which is
        what makes a refresh safe and a rewritten recall impossible.
        """
        with self._lock:
            step = self._require_current_step(component_id)
            if not step.reveal_required:
                raise SessionTransitionError("type_not_revealable")

            back = disclose()

            frozen = self._revealed.setdefault(step.component_id, attempt)
            return RevealOutcome(back=back, attempt=frozen)

    def resume_from(self, previous: MiniAppSession) -> None:
        """Take over from the session this one replaces.

        What a reload is: the learner's place in the exercise moves onto their
        new token. Without it, a reload would hand back a token that works and
        an exercise that has forgotten them — which silently discards work.

        Exactly the resumable state moves: position, responses, frozen reveal
        attempts, the completion timestamp, and the cached result. The scored
        result comes with the completion that produced it, because carrying one
        without the other leaves the replacement *finished but unscored* —
        finished enough for the completion screen to accept it, unscored enough
        to compute the attempt again, and a second durable record for one
        learner session.

        Nothing that *bounds* a session is inherited: not the token digest, not
        the ``initData`` freshness, not the expiry, not the creation time. Those
        belong to this session, and copying them would make a reload a way to
        extend a session indefinitely. The copies are deep, so the retired
        session and its replacement cannot write through each other.
        """
        if previous is self:
            return
        first, second = sorted((self, previous), key=id)
        with first._lock, second._lock:
            if self.scope != previous.scope or self._steps != previous._steps:
                raise ValueError("sessions may resume only across the same scope and ordered plan")
            if float(previous._clock()) >= previous._hard_expires_at:
                raise ValueError("the predecessor is not resumable")
            if previous._retired and not previous._capacity_evicted:
                raise ValueError("the predecessor is not resumable")
            if (
                self._position != 0
                or self._responses
                or self._revealed
                or self._completed_at is not None
                or self._attempt_result is not None
                or self._retired
            ):
                raise ValueError("the replacement session must be fresh")
            (
                self._position,
                self._responses,
                self._revealed,
                self._completed_at,
                self._attempt_result,
            ) = (
                previous._position,
                deepcopy(previous._responses),
                dict(previous._revealed),
                previous._completed_at,
                deepcopy(previous._attempt_result),
            )
            # Transfer and retirement are one two-session commit. Leaving
            # retirement to the caller created a gap where a stale request
            # could score the predecessor after its state had been copied.
            previous._retired = True
            previous.expires_at = 0.0

    def score_once(self, compute) -> dict[str, Any]:
        """Produce this session's one durable result, computing it exactly once.

        ``compute`` receives a :class:`SessionCompletion` — the responses and the
        two timestamps, copied — and is injected because scoring, mastery, and
        review persistence belong with the evaluation runtime rather than here.

        It runs the first time and never again: a refresh, a duplicate request, a
        backgrounded webview resuming must see the identical result rather than
        score the session a second time and write a second row for it. Only a
        *successful* computation is cached, so a transient failure leaves the
        session finished and still scorable instead of permanently unscored.
        """
        with self._lock:
            self._ensure_active()
            if self._completed_at is None:
                raise SessionTransitionError("not_completed")
            if self._attempt_result is None:
                computed = compute(
                    SessionCompletion(
                        responses=deepcopy(self._responses),
                        created_at=self.created_at,
                        completed_at=self._completed_at,
                    )
                )
                self._attempt_result = deepcopy(computed)
            assert self._attempt_result is not None
            return deepcopy(self._attempt_result)


class SessionStore:
    """A bounded, in-memory map of token digest to session."""

    def __init__(self, *, ttl_seconds: int, max_sessions: int, clock) -> None:
        self._ttl = int(ttl_seconds)
        self._max = int(max_sessions)
        self._clock = clock
        self._sessions: dict[str, MiniAppSession] = {}
        self._last_activity: float | None = None

    def __len__(self) -> int:
        return len(self._sessions)

    @property
    def last_activity_at(self) -> float | None:
        """When an authenticated learner was last served, or ``None``.

        Recorded on the store rather than on a session because it outlives
        every individual one. A learner who works for an hour holds several
        sessions in succession, and the runtime's idle timer must see that as
        continuous use rather than as a series of sessions that each went quiet.

        Deliberately *not* updated by unauthenticated traffic. Once the runtime
        is reachable through a public tunnel, anything on the internet can
        knock on the door; treating a scanner as a learner would keep a public
        entrance to somebody's learning record open for as long as the scanning
        continued.
        """
        return self._last_activity

    def note_activity(self) -> None:
        """Record that an authenticated learner request was just served."""
        self._last_activity = float(self._clock())

    def create(
        self, scope: SessionScope, *, steps: tuple[SessionStep, ...], auth_date: int = 0
    ) -> tuple[str, MiniAppSession]:
        """Mint a session over an ordered plan and return ``(token, session)``.

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
            _steps=tuple(steps),
            _clock=self._clock,
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
            session.retire()
            raise SessionError("session_expired")
        if session.scope.profile != profile:
            raise SessionError("session_wrong_profile")
        if session.scope.telegram_user_id != str(telegram_user_id):
            # A stolen or shared token is useless without the Telegram identity
            # it was minted for.
            raise SessionError("session_wrong_user")
        return session

    def drop(self, token: str) -> None:
        session = self._sessions.pop(_digest(token), None)
        if session is not None:
            session.retire()

    def purge_expired(self) -> int:
        now = float(self._clock())
        stale = [digest for digest, s in self._sessions.items() if s.expired(now)]
        for digest in stale:
            session = self._sessions.pop(digest)
            session.retire()
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
            session = self._sessions.pop(oldest)
            # GrantStore holds the object, not its token.  Retiring in place
            # keeps both stores' view of liveness identical after eviction.
            session.retire_for_capacity()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
