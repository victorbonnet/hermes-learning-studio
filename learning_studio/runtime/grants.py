"""The expiring permission that turns "a button was sent" into "this may open".

A grant is what a launch actually creates. It lives inside the runtime process,
it expires, and it names exactly one of everything: one profile, one runtime
generation, one Telegram account, one learner, one experience.

Why a grant rather than a token in the URL
------------------------------------------

The obvious design is to mint a session at launch and put its token in the URL
behind the button. That URL is then a bearer credential in a link — it goes
into Telegram's servers, the webview's history, and any screenshot of the chat.
This plugin's rule is that session tokens never enter URLs, so the button
carries the tunnel address and nothing else.

The grant is the other half. When the webview bootstraps, it presents verified
``initData`` as it always did; what changes is that the runtime additionally
requires an unexpired grant for *that* Telegram account and *that* experience.
So the button is not a credential, and a stranger who obtains the tunnel
address still has to be the account the launch was for.

That makes the API strictly stricter than before, and only where a grant store
is wired in — an operator who runs the server themselves gets exactly the
behaviour they had. There is no setting that disables grants in a runtime this
plugin launched.

What a grant is not
-------------------

It is not a record of performance. It holds a reference to the session minted
under it so that a launch can be *reported on* honestly — opened or not, how
far through, finished or not — and that is the whole of it. Nothing here is
written to disk, nothing survives the process, and nothing is scored.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: How long a learner has to tap the button before the grant expires. Short:
#: it is the window between "a message arrived" and "they opened it", not the
#: length of a study session — the session's own TTL governs that.
#:
#: Deliberately shorter than
#: :data:`learning_studio.consent.CONSENT_TTL_SECONDS`. The two windows measure
#: different things — how long a button stays live, and how long an agreement
#: stays relevant — and making them equal would collapse "they never opened it,
#: ask before trying again" into "they agreed too long ago", which is a
#: different thing to tell the learner.
DEFAULT_GRANT_TTL_SECONDS = 600

#: Upper bound on live grants, so a runtime cannot be made to hold unbounded
#: state by a caller that only ever creates them.
MAX_GRANTS = 200

_ID_BYTES = 12


class GrantError(ValueError):
    """The control payload was not a grant this runtime can create."""


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
    #: The session minted when the learner actually opened it. Held as the
    #: object rather than as an identifier so that reporting progress is a read
    #: of live state and cannot become a second, drifting copy of it.
    session: Any = field(default=None, repr=False)

    def expired(self, now: float) -> bool:
        return now >= self.expires_at

    def usable(self, now: float) -> bool:
        """A grant that may still admit its learner.

        Once opened, the grant has done its job: the session it minted is what
        governs access from then on, and its own expiry is what ends it.
        """
        return not self.revoked and not self.expired(now)

    def state(self, now: float) -> str:
        if self.revoked:
            return "revoked"
        session = self.session
        if session is not None:
            return "completed" if getattr(session, "completed", False) else "opened"
        return "expired" if self.expired(now) else "waiting"


class GrantStore:
    """Bounded, in-memory, and scoped to one runtime generation."""

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
        self._grants: dict[str, LaunchGrant] = {}

    def __len__(self) -> int:
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
        what turns stale consent into a refusal rather than a fresh tunnel.
        """
        telegram_user_id = _identifier(payload.get("telegram_user_id"), "telegram_user_id")
        learner_id = _identifier(payload.get("learner_id"), "learner_id")
        experience_id = _identifier(payload.get("experience_id"), "experience_id")
        reuse_only = bool(payload.get("reuse_only"))

        now = float(self._clock())
        self.purge(now)

        existing = self._find(telegram_user_id, experience_id, now)
        if existing is not None:
            return {"reused": True, "created": False, **self._describe(existing, now)}
        if reuse_only:
            return {"reused": False, "created": False}

        self._enforce_capacity()
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

    def revoke(self, launch_id: str) -> bool:
        """Withdraw a grant, and end the session it minted if there is one.

        Both halves are the rollback: a launch whose button never arrived must
        leave nothing a learner could open, and if one had somehow already been
        opened, nothing they could continue.
        """
        grant = self._grants.pop(str(launch_id), None)
        if grant is None:
            return False
        grant.revoked = True
        session = grant.session
        if session is not None:
            # Expiring in place rather than reaching into the session store:
            # the session resolves against its own expiry on every request, so
            # this is enough to end it and needs no second lookup.
            session.expires_at = 0.0
        return True

    def progress(self, payload: dict[str, Any]) -> dict[str, Any]:
        """What actually happened, with nothing invented.

        Looked up by ``(telegram user, experience)`` rather than by launch id,
        so the caller cannot ask about a launch it does not own: the account is
        part of the key, and it comes from the trusted session context on the
        other side of the control plane, never from a tool payload.

        There is no score here, and no attempt. The only facts a runtime holds
        are whether the learner opened the exercise, which component they are
        on, how many they have answered, and whether they reached the end — and
        those are the only facts reported.
        """
        telegram_user_id = _identifier(payload.get("telegram_user_id"), "telegram_user_id")
        experience_id = _identifier(payload.get("experience_id"), "experience_id")

        now = float(self._clock())
        for grant in self._grants.values():
            if grant.telegram_user_id == telegram_user_id and grant.experience_id == experience_id:
                return {"found": True, **self._describe(grant, now)}
        return {"found": False}

    # -- the request path ------------------------------------------------

    def admit(self, *, telegram_user_id: str, experience_id: str) -> LaunchGrant | None:
        """The grant admitting this account to this exercise, or ``None``."""
        now = float(self._clock())
        self.purge(now)
        return self._find(str(telegram_user_id), str(experience_id), now)

    def bind_session(self, grant: LaunchGrant, session: Any) -> None:
        """Record the session a grant admitted, so a launch can be reported on."""
        grant.session = session

    # -- housekeeping ----------------------------------------------------

    def purge(self, now: float | None = None) -> int:
        moment = float(self._clock()) if now is None else now
        stale = [
            launch_id
            for launch_id, grant in self._grants.items()
            if grant.expired(moment) and grant.session is None
        ]
        for launch_id in stale:
            del self._grants[launch_id]
        return len(stale)

    def _find(self, telegram_user_id: str, experience_id: str, now: float) -> LaunchGrant | None:
        for grant in self._grants.values():
            if (
                grant.telegram_user_id == telegram_user_id
                and grant.experience_id == experience_id
                and grant.usable(now)
            ):
                return grant
        return None

    def _enforce_capacity(self) -> None:
        while len(self._grants) >= self._max:
            oldest = min(self._grants, key=lambda key: self._grants[key].expires_at)
            del self._grants[oldest]

    def _describe(self, grant: LaunchGrant, now: float) -> dict[str, Any]:
        """The reply shape. Deliberately without the learner's answers.

        A learner's responses are in the session object this reads from. None
        of them travels: an agent is told how far somebody got, never what they
        said.
        """
        session = grant.session
        return {
            "launch_id": grant.launch_id,
            "generation": grant.generation,
            "state": grant.state(now),
            "opened": session is not None,
            "expires_in_seconds": max(0.0, grant.expires_at - now),
            "position": int(getattr(session, "position", 0) or 0),
            "component_count": int(getattr(session, "component_count", 0) or 0),
            "answered": len(getattr(session, "answers", ()) or ()),
            "completed": bool(getattr(session, "completed", False)),
            # Stated in the payload rather than left to a reader's assumption.
            "scored": False,
        }


def _identifier(raw: Any, label: str) -> str:
    if not isinstance(raw, str):
        raise GrantError(f"{label} must be a string")
    text = raw.strip()
    if not text or len(text) > 128:
        raise GrantError(f"{label} is not a usable identifier")
    return text
