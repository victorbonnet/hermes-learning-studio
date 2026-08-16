"""The expiring permission that turns "a button was sent" into "this may open".

A grant is what a launch actually creates. It lives inside the runtime process,
it expires, and it names exactly one of everything: one profile, one runtime
generation, one Telegram account, one learner, one experience.

Why the button carries a selector and not a token
-------------------------------------------------

The obvious design is to mint a session at launch and put its token in the URL
behind the button. That URL then goes into Telegram's servers, the webview's
history, and any screenshot of the chat — so the token is a bearer credential
in a link, which this plugin does not do.

The obvious *alternative* is to put nothing in the URL and let the frontend ask
for "the exercise". That is what the first version did, and it did not work at
all: an inline ``web_app`` button produces no ``start_param``, so the page had
nothing to open, and the launch only appeared to succeed because a test posted
an experience id the real client never had.

So the button carries the **launch id**: opaque, random, expiring, and useless
on its own. It goes in the URL *fragment*, which browsers never send to the
server — not in the request line, not in ``Referer`` — so it stays out of logs
and out of the tunnel operator's view, and the frontend strips it from history
the moment it has read it.

**It is a selector, not a capability.** Presenting it says only *which* launch
is meant. Opening still requires verified ``initData``, an account on the
allowlist, ownership of the experience in SQL, and a grant that is unexpired,
unrevoked, and issued to that same account in this runtime generation. A copied
URL, on its own, opens nothing.

One launch, one session
-----------------------

A grant has at most one *live* session at a time, and re-admission is how a
reload works: the new session inherits the position, answers, and reveals of
the one it replaces, and the replaced one is expired on the spot. So there is
never a second token that still works, progress never restarts, and the
question "which session is this launch?" always has one answer.

That matters more than it sounds. Without it, two ``POST /api/session`` calls
minted two live tokens, the grant's pointer named only the newer, and revoking
the launch left the older one working.

What a grant is not
-------------------

It is not a record of performance. It holds the session so a launch can be
*reported on* honestly — opened or not, how far through, finished or not — and
that is the whole of it. Nothing here is written to disk, nothing survives the
process, and nothing is scored.
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: How long a learner has to tap the button before the grant expires. Short:
#: it is the window between "a message arrived" and "they opened it", not the
#: length of a study session — the session's own TTL governs that.
#:
#: Deliberately shorter than
#: :data:`learning_studio.evidence.EVIDENCE_TTL_SECONDS`, so a button lapsing
#: is never mistaken for the learner's message lapsing.
DEFAULT_GRANT_TTL_SECONDS = 600

#: Upper bound on live grants. Reaching it *refuses* a new launch rather than
#: evicting an existing one — see :meth:`GrantStore._make_room`.
MAX_GRANTS = 200

#: Bytes of entropy in a launch id. It is not a capability, but it is the thing
#: a caller names, so guessing one must not be a strategy.
_ID_BYTES = 18

#: The exact shape of a launch id, anchored. Used on both sides: the runtime
#: refuses anything else, and the frontend refuses to send anything else.
LAUNCH_ID_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{16,64}\Z")


class GrantError(ValueError):
    """The control payload was not a grant this runtime can create."""


class GrantCapacityError(GrantError):
    """No room for another launch without retiring one somebody is using."""


@dataclass
class LaunchGrant:
    """One launch, and what became of it."""

    launch_id: str
    profile: str
    generation: int
    telegram_user_id: str
    learner_id: str
    experience_id: str
    created_at: float
    expires_at: float
    revoked: bool = False
    #: False until the button has actually been delivered.
    #:
    #: A grant is created *before* the message is sent, because the selector
    #: has to exist in order to go in it. Until delivery succeeds it admits
    #: nobody: otherwise a send that failed would leave a working entrance
    #: behind whose address the learner never received but which is live all
    #: the same.
    activated: bool = False
    #: The one live session, or ``None``. Held as the object rather than as an
    #: identifier so that reporting progress is a read of live state and cannot
    #: become a second, drifting copy of it.
    session: Any = field(default=None, repr=False)
    #: Every session ever minted under this grant, live or retired. Revocation
    #: expires all of them — a pointer to the newest would leave an older token
    #: working, which is exactly what it used to do.
    sessions: list = field(default_factory=list, repr=False)

    def expired(self, now: float) -> bool:
        return now >= self.expires_at

    def live_session(self, now: float) -> Any:
        """The session this grant is holding, if it is still usable.

        A *pointer* to a session is not a session. The pointer outlives the
        thing it names — retirement sets an expiry, it does not clear the
        field — so asking "is there a session?" answered yes forever, and every
        rule built on that question inherited the mistake.
        """
        session = self.session
        if session is None:
            return None
        expires_at = getattr(session, "expires_at", None)
        if expires_at is None or now >= float(expires_at):
            return None
        return session

    def admissible(self, now: float) -> bool:
        """May still admit a learner who taps the button.

        The button's window closes at ``expires_at`` — but only for a launch
        nobody has opened. Once a session exists, that session's own lifetime
        governs, so a learner working through an exercise is not thrown out
        because the *invitation* went stale mid-question.

        A **live** session, though. The previous version accepted any session
        pointer, so a launch that had been opened once stayed admissible for
        ever: the button's window had passed, the session had passed, and the
        grant still let a fresh one be minted. Two expiries that had both run
        out added up to no expiry at all.
        """
        if self.revoked or not self.activated:
            return False
        if self.live_session(now) is not None:
            return True
        return not self.expired(now)

    def state(self, now: float) -> str:
        if self.revoked:
            return "revoked"
        if not self.activated:
            return "pending"
        session = self.live_session(now)
        if session is not None:
            return "completed" if getattr(session, "completed", False) else "opened"
        if self.session is not None:
            # Opened, and the session has since run out. Distinct from
            # ``expired``, which is a button nobody ever tapped: this learner
            # did open the exercise, and saying otherwise would misreport what
            # happened.
            return "closed"
        return "expired" if self.expired(now) else "waiting"


class GrantStore:
    """Bounded, in-memory, scoped to one runtime generation, and locked.

    The lock is not decoration. Two ``POST /api/session`` calls arriving
    together — a double tap, a webview that retries — must not both mint a live
    session, and the arbitration has to happen somewhere.
    """

    def __init__(
        self,
        *,
        profile: str,
        generation: int,
        clock=time.time,
        ttl_seconds: int = DEFAULT_GRANT_TTL_SECONDS,
        max_grants: int = MAX_GRANTS,
    ) -> None:
        self._profile = profile
        self._generation = int(generation)
        self._clock = clock
        self._ttl = int(ttl_seconds)
        self._max = int(max_grants)
        self._lock = threading.RLock()
        self._grants: dict[str, LaunchGrant] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._grants)

    # -- the control-plane surface ---------------------------------------

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a grant, or hand back the compatible one already here.

        Idempotent on ``(telegram user, experience)`` by design. A second
        launch of the same exercise for the same learner is a repeat, not a
        second exercise, and answering it with the existing grant is what stops
        a retry from opening a second public entrance — and what lets the
        caller tell "this is a repeat" from "this is new" and decline to send
        another message.

        ``reuse_only`` is how the caller says *this may not become a new
        launch*: with nothing to reuse it creates nothing and says so, which is
        what turns a spent message into a refusal rather than a fresh tunnel.
        """
        telegram_user_id = _identifier(payload.get("telegram_user_id"), "telegram_user_id")
        learner_id = _identifier(payload.get("learner_id"), "learner_id")
        experience_id = _identifier(payload.get("experience_id"), "experience_id")
        reuse_only = bool(payload.get("reuse_only"))

        with self._lock:
            now = float(self._clock())
            self._purge(now)

            existing = self._find(telegram_user_id, experience_id, now)
            if existing is not None:
                return {"reused": True, "created": False, **self._describe(existing, now)}
            if reuse_only:
                return {"reused": False, "created": False}

            self._make_room(now)
            grant = LaunchGrant(
                launch_id=secrets.token_urlsafe(_ID_BYTES),
                profile=self._profile,
                generation=self._generation,
                telegram_user_id=telegram_user_id,
                learner_id=learner_id,
                experience_id=experience_id,
                created_at=now,
                expires_at=now + self._ttl,
            )
            self._grants[grant.launch_id] = grant
            return {"reused": False, "created": True, **self._describe(grant, now)}

    def activate(self, launch_id: str) -> bool:
        """Commit a launch: from here it admits its learner.

        Called once the button has actually been delivered, which is the only
        moment at which an entrance somebody can reach should start existing.
        Returns False for a launch this store no longer has — a caller that
        sent a message and then could not commit is in an indeterminate state
        and has to say so rather than report success.
        """
        with self._lock:
            now = float(self._clock())
            grant = self._grants.get(str(launch_id))
            if grant is None or grant.revoked:
                return False
            if grant.expired(now):
                # The button's window closed while the message was in flight.
                # Activating anyway produced the worst possible pair of facts:
                # a launch reported as open, and a button that admits nobody.
                # Refusing here makes the caller report what is true — it could
                # not be committed — and roll the grant back.
                return False
            grant.activated = True
            return True

    def revoke(self, launch_id: str) -> bool:
        """Withdraw a grant, and end **every** session it ever minted.

        Both halves are the rollback: a launch whose button never arrived must
        leave nothing a learner could open, and nothing they could continue.
        Expiring only the newest session — which is what a single pointer got
        you — left every earlier token working.
        """
        with self._lock:
            grant = self._grants.pop(str(launch_id), None)
            if grant is None:
                return False
            grant.revoked = True
            self._retire_all(grant)
            return True

    def progress(self, payload: dict[str, Any]) -> dict[str, Any]:
        """What actually happened, with nothing invented.

        Looked up by ``(telegram user, experience)`` rather than by launch id,
        so the caller cannot ask about a launch it does not own: the account is
        part of the key, and it comes from the trusted session context on the
        other side of the control plane, never from a tool payload.

        There is no score here, and no attempt. The only facts a runtime holds
        are whether the learner opened the exercise, which component they are
        on, how many they have answered, and whether they reached the end.
        """
        telegram_user_id = _identifier(payload.get("telegram_user_id"), "telegram_user_id")
        experience_id = _identifier(payload.get("experience_id"), "experience_id")

        with self._lock:
            now = float(self._clock())
            grant = self._select(telegram_user_id, experience_id, now, admissible_only=False)
            if grant is None:
                return {"found": False}
            return {"found": True, **self._describe(grant, now)}

    # -- the request path ------------------------------------------------

    def admit(self, *, launch_id: str, telegram_user_id: str) -> LaunchGrant | None:
        """The grant this selector names, for this account, or ``None``.

        Everything that could make it not theirs is checked here: the shape of
        the identifier, the account it was issued to, the generation it belongs
        to, revocation, and expiry. A caller holding somebody else's copied URL
        gets ``None``, and so does a caller holding their own expired one.
        """
        selector = str(launch_id or "")
        if not LAUNCH_ID_PATTERN.match(selector):
            return None
        with self._lock:
            now = float(self._clock())
            self._purge(now)
            grant = self._grants.get(selector)
            if grant is None:
                return None
            if grant.telegram_user_id != str(telegram_user_id):
                return None
            if grant.generation != self._generation:
                return None
            if not grant.admissible(now):
                return None
            return grant

    def admit_session(self, grant: LaunchGrant, create):
        """Give this grant its one live session, or ``None`` if it is no longer
        admissible.

        ``create`` is called *under the lock* and returns ``(token, session)``.
        That is what makes two simultaneous admissions safe: they serialise
        here, and the second one sees the first one's session and carries its
        state forward instead of starting a parallel one.

        A reload therefore resumes rather than restarts, and the token it
        replaces stops working immediately — so there is never a second live
        token for one launch.

        Every condition :meth:`admit` checked is checked **again** here, under
        the lock, because the caller had to let go of it in between: it has an
        ownership query and a bundle load to do, and a revocation landing in
        that window used to be lost. The grant was popped and its sessions
        retired, then this method attached a brand-new live session to the
        object the caller was still holding — leaving a working token for a
        launch the store no longer had, which no later revocation could reach.

        Identity, not equality: ``is`` compares against the object currently in
        the store, so a grant that was revoked and whose selector was somehow
        reissued cannot pass by looking alike.
        """
        with self._lock:
            now = float(self._clock())
            if self._grants.get(grant.launch_id) is not grant:
                return None
            if grant.revoked or grant.generation != self._generation:
                return None
            if not grant.admissible(now):
                return None
            # Never resurrect state after the hard session expiry.  The raw
            # pointer intentionally outlives the token for truthful history,
            # but only a live session may be resumed.
            previous = grant.live_session(now)
            token, session = create()
            if previous is not None:
                _carry_forward(previous, session)
                _retire(previous)
            grant.session = session
            grant.sessions.append(session)
            return token, session

    # -- housekeeping ----------------------------------------------------

    def purge(self, now: float | None = None) -> int:
        with self._lock:
            return self._purge(float(self._clock()) if now is None else now)

    def _purge(self, now: float) -> int:
        """Drop launches nobody can use. The caller holds the lock.

        A grant with a live session is never purged here whatever its own
        expiry says — the button's window and the session's lifetime are
        different clocks, and the learner is inside the second one.
        """
        stale = [
            launch_id
            for launch_id, grant in self._grants.items()
            # A pending grant is kept while the transaction that created it is
            # still plausibly running, and dropped once its own window has
            # passed. Keeping it unconditionally would leak: a launch killed
            # between "create" and "activate or revoke" leaves one behind that
            # nothing will ever resolve, and it would count against capacity
            # for the life of the runtime.
            if _abandoned(grant, now)
            or (grant.activated and not grant.admissible(now))
            or _finished(grant, now)
        ]
        for launch_id in stale:
            self._retire_all(self._grants.pop(launch_id))
        return len(stale)

    def _make_room(self, now: float) -> None:
        """Refuse rather than retire something somebody is using.

        Eviction by expiry — which is what this did — could throw out an
        *active* learner's grant while their session stayed alive, leaving a
        session nothing could report on or revoke. A refusal is worse for the
        person who arrives second and correct for the person already working.
        """
        if len(self._grants) < self._max:
            return
        self._purge(now)
        if len(self._grants) >= self._max:
            raise GrantCapacityError("this runtime is already serving as many launches as it can")

    def _find(self, telegram_user_id: str, experience_id: str, now: float) -> LaunchGrant | None:
        return self._select(telegram_user_id, experience_id, now, admissible_only=True)

    def _select(
        self,
        telegram_user_id: str,
        experience_id: str,
        now: float,
        *,
        admissible_only: bool,
    ) -> LaunchGrant | None:
        """The one grant that answers for this learner and exercise.

        Chosen by an explicit rule — an open one first, then the most recently
        created — rather than by whichever the dictionary happened to yield.
        Insertion order is not a decision, and when two grants existed it made
        "which launch is this?" depend on history nobody could see.
        """
        candidates = [
            grant
            for grant in self._grants.values()
            if grant.telegram_user_id == telegram_user_id
            and grant.experience_id == experience_id
            and (grant.admissible(now) if admissible_only else True)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda g: (g.session is not None, g.created_at))

    def _retire_all(self, grant: LaunchGrant) -> None:
        for session in grant.sessions:
            _retire(session)
        grant.session = None

    def _describe(self, grant: LaunchGrant, now: float) -> dict[str, Any]:
        """The reply shape. Deliberately without the learner's answers.

        A learner's responses are in the session object this reads from. None
        of them travels: an agent is told how far somebody got, never what they
        said.
        """
        # The live one, so progress stops being reported the moment the
        # session it came from stops existing. A retired session kept answering
        # "position 3 of 10" indefinitely, which is a learner's activity
        # outliving the thing that was supposed to bound it.
        session = grant.live_session(now)
        return {
            "launch_id": grant.launch_id,
            "generation": grant.generation,
            "state": grant.state(now),
            "opened": grant.session is not None,
            "expires_in_seconds": max(0.0, grant.expires_at - now),
            "position": int(getattr(session, "position", 0) or 0),
            "component_count": int(getattr(session, "component_count", 0) or 0),
            "answered": len(getattr(session, "answers", ()) or ()),
            "completed": bool(getattr(session, "completed", False)),
            # Stated in the payload rather than left to a reader's assumption.
            "scored": False,
        }


def _abandoned(grant: LaunchGrant, now: float) -> bool:
    """A pending grant whose transaction never finished either way."""
    return not grant.activated and grant.expired(now)


def _finished(grant: LaunchGrant, now: float) -> bool:
    """A grant whose session has expired and which can admit nobody new."""
    session = grant.session
    if session is None:
        return False
    return bool(session.expired(now)) and grant.expired(now)


def _retire(session: Any) -> None:
    """End a session in place.

    Setting the expiry is enough and needs no second lookup: the session store
    checks it on every resolution, so the token stops working immediately
    wherever it is held.
    """
    if getattr(session, "expires_at", None) is not None:
        session.expires_at = 0.0


def _carry_forward(previous: Any, session: Any) -> None:
    """Move a learner's place in the exercise onto their new session.

    Without this, a reload would hand back a token that works and an exercise
    that has forgotten them — which is a worse failure than the parallel
    sessions it replaces, because it silently discards work.

    The scored result comes with the completion that produced it. Carrying one
    without the other left the replacement *finished but unscored*: finished
    enough for the completion screen to accept it, unscored enough to compute
    the attempt again — and a second durable record for one learner session.
    """
    session.position = getattr(previous, "position", 0)
    session.answers = dict(getattr(previous, "answers", {}) or {})
    session.revealed = dict(getattr(previous, "revealed", {}) or {})
    session.completed_at = getattr(previous, "completed_at", None)
    session.attempt_result = getattr(previous, "attempt_result", None)


def _identifier(raw: Any, label: str) -> str:
    if not isinstance(raw, str):
        raise GrantError(f"{label} must be a string")
    text = raw.strip()
    if not text or len(text) > 128:
        raise GrantError(f"{label} is not a usable identifier")
    return text
