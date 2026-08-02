"""When a launch may happen without asking, and when it may not.

Opening a Mini App is not like storing a preference. It creates a temporary
public address on the learner's behalf and sends them a message. So the
question this module answers is narrow and worth stating precisely:

**A learner who asked for practice has already consented.** "Can we revise
photosynthesis?", "quiz me on chapter four", "let me try some of those again" —
all of these *are* the request. Stopping to ask "shall I open an exercise?"
after one of them is friction dressed up as consent, and it teaches the learner
that the assistant does not listen.

**A suggestion the learner did not make needs a yes.** When the agent proposes
practice unprompted, nothing has been consented to yet, and a public entrance
must not appear because an assistant had an idea.

**Consent is for one launch, not for an experience.** This is the rule that
makes retries safe. An agreement is recorded when it is used, and a *second*
new launch of the same exercise on the strength of that same agreement is
refused. A repeat call is still allowed to return the launch that is already
open — reporting on something that exists is not a new act — but it may not
quietly create a second one.

What this module cannot do, and says so
---------------------------------------

It cannot verify that the learner agreed. ``learner_confirmed`` is written by
the model, in the same call as everything else, so it is an assertion and not
proof — the same limitation that governs ``track.confirmed`` elsewhere in this
plugin. What the rule buys is not a guarantee; it is that an agent has to
*state* that a specific person agreed to a specific thing, once, and cannot
reuse that statement to keep opening tunnels. The quote is required for the
same reason: writing down what somebody said is a different act from ticking a
box, and it is legible to whoever reads the conversation afterwards.

The ledger is in memory and per-process. A Hermes restart forgets it, and the
effect of forgetting is that the next agent-initiated launch asks again — the
safe direction.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

#: How long a recorded agreement stays relevant. Beyond this the learner has
#: moved on, and re-opening on the strength of it would surprise them.
CONSENT_TTL_SECONDS = 900

#: Bound on the ledger, so a long session cannot grow it without limit.
MAX_ENTRIES = 200

#: The two ways a launch can be initiated. Named rather than boolean, so the
#: tool schema asks the agent to say which happened instead of inviting it to
#: leave a flag unset.
LEARNER_REQUEST = "learner_request"
AGENT_SUGGESTION = "agent_suggestion"
INITIATIONS = (LEARNER_REQUEST, AGENT_SUGGESTION)

#: Shortest quote that could plausibly record what somebody said.
MIN_QUOTE_CHARS = 2
MAX_QUOTE_CHARS = 500


class ConsentRequired(Exception):
    """The launch was refused on consent grounds. The message is agent-facing."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason


NOT_CONFIRMED = (
    "You suggested this exercise rather than the learner asking for it, so it needs their "
    "agreement before anything is opened or sent. Describe it in one line, ask, and call "
    "this again with what they said. Nothing was started."
)

QUOTE_REQUIRED = (
    "An exercise you suggested needs the learner's own words recorded alongside the "
    "confirmation. Nothing was started."
)

ALREADY_USED = (
    "The learner's agreement to that suggestion has already been used to open this exercise "
    "once, and the earlier session is no longer open. Ask them again before starting a new "
    "one. Nothing was started."
)

STALE = (
    "The learner agreed to this a while ago and the conversation has moved on. Ask again "
    "before opening it. Nothing was started."
)


@dataclass(frozen=True)
class Decision:
    """What the policy concluded, and what the caller may therefore do."""

    initiation: str
    #: False when the caller may only return a launch that already exists.
    may_create: bool
    #: Recorded in the response so the conversation shows why it proceeded.
    basis: str


@dataclass
class _Entry:
    used_at: float
    quote: str


@dataclass
class ConsentLedger:
    """Agreements this process has already spent, per learner and exercise."""

    clock: object = time.time
    ttl_seconds: int = CONSENT_TTL_SECONDS
    _entries: dict[tuple[str, str, str], _Entry] = field(default_factory=dict, repr=False)

    def decide(
        self,
        *,
        profile: str,
        learner_scope: str,
        experience_id: str,
        initiation: str,
        learner_confirmed: bool,
        confirmation_quote: str | None,
    ) -> Decision:
        """Apply the policy, or raise :class:`ConsentRequired`.

        Nothing is recorded here. The caller records the agreement with
        :meth:`spend` only once a launch has actually been created, so a
        refusal further down the sequence — no runtime, no tunnel, no delivery —
        does not consume an agreement that never produced anything.
        """
        if initiation not in INITIATIONS:
            raise ConsentRequired(NOT_CONFIRMED, reason="initiation_unknown")

        if initiation == LEARNER_REQUEST:
            return Decision(
                initiation=initiation,
                may_create=True,
                basis="The learner asked for this exercise, so it started without a second ask.",
            )

        if not learner_confirmed:
            raise ConsentRequired(NOT_CONFIRMED, reason="suggestion_not_confirmed")
        quote = (confirmation_quote or "").strip()
        if not MIN_QUOTE_CHARS <= len(quote) <= MAX_QUOTE_CHARS:
            raise ConsentRequired(QUOTE_REQUIRED, reason="confirmation_quote_missing")

        key = (profile, learner_scope, experience_id)
        entry = self._entries.get(key)
        if entry is None:
            return Decision(
                initiation=initiation,
                may_create=True,
                basis="The learner agreed to the exercise you suggested.",
            )

        now = float(self.clock())
        if now - entry.used_at > self.ttl_seconds:
            del self._entries[key]
            raise ConsentRequired(STALE, reason="confirmation_stale")

        # Within the window, and already spent. A repeat may still return the
        # launch that is open; it may not become a second one.
        return Decision(
            initiation=initiation,
            may_create=False,
            basis="Reporting the exercise that is already open for this learner.",
        )

    def spend(self, *, profile: str, learner_scope: str, experience_id: str, quote: str) -> None:
        """Record that an agreement has now produced a launch."""
        self._purge()
        self._entries[(profile, learner_scope, experience_id)] = _Entry(
            used_at=float(self.clock()), quote=quote[:MAX_QUOTE_CHARS]
        )

    def _purge(self) -> None:
        now = float(self.clock())
        stale = [
            key for key, entry in self._entries.items() if now - entry.used_at > self.ttl_seconds
        ]
        for key in stale:
            del self._entries[key]
        while len(self._entries) >= MAX_ENTRIES:
            oldest = min(self._entries, key=lambda key: self._entries[key].used_at)
            del self._entries[oldest]


#: One ledger per Hermes process. Keyed by profile and learner inside, so two
#: profiles served by one process cannot see each other's agreements.
LEDGER = ConsentLedger()
