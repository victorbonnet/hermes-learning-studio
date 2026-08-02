"""When a launch may happen, decided from what the learner actually said.

Opening an exercise creates a temporary public address and sends somebody a
message, so the authority to do it has to come from somewhere better than the
model's own account of the conversation.

Two questions, two different sources
------------------------------------

**"Did the learner say this?"** is a question of fact, and the model is not the
source. It is answered by :mod:`learning_studio.evidence`, which recorded the
incoming message before the agent ran and keys it by exactly which message it
was. The quotation the tool is given must appear in *that* message.

**"Did they mean 'open an exercise'?"** is a question of interpretation, and
the model is exactly the right source. "Quiz me on chapter four" and "go on
then, let's try some" are consent; "I'm tired" is not; and no rule this module
could write would read those better than the agent already does.

So the model classifies, and the host supplies the words. Neither alone is
enough, which is the point.

The two rules
-------------

**A learner who asked has already agreed.** Stopping to ask "shall I open an
exercise?" after "quiz me on this" is friction dressed up as consent, and it
teaches the learner that the assistant does not listen.

**A suggestion the learner did not make needs a yes** — and that yes has to be
a message they sent, not a summary of one.

Both require evidence. The difference between them is what the evidence has to
contain: a request, or an agreement to a proposal.

One message, one launch
-----------------------

Authority is spent when a launch commits, and the spending is atomic — two
concurrent calls on one message do not both win. A repeat call may still
*return the launch that is already open*, because reporting on something that
exists is not a new act; it may not create a second one.

A spent message stays spent. The earlier version of this deleted its own record
when the entry went stale and then raised — so the very next call found nothing,
concluded the consent was fresh, and launched. Expiry now leaves a tombstone
that outlives the evidence, which is what makes "stale" a terminal state rather
than a step on the way back to "new".
"""

from __future__ import annotations

from dataclasses import dataclass

from .evidence import STORE, EvidenceKey, EvidenceStore

#: The two ways a launch can be initiated. Named rather than boolean, so the
#: tool schema asks the agent to say which happened instead of inviting it to
#: leave a flag unset.
LEARNER_REQUEST = "learner_request"
AGENT_SUGGESTION = "agent_suggestion"
INITIATIONS = (LEARNER_REQUEST, AGENT_SUGGESTION)

#: Bounds on the quotation itself. The lower bound is not a security property —
#: the message identity carries that — but a two-character quotation would tell
#: a reader nothing about whether the model had read the message.
MIN_QUOTE_CHARS = 4
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
    "this again once they have answered — quoting what they said. Nothing was started."
)

NO_EVIDENCE = (
    "There is no current learner message to launch from. An exercise opens in response to "
    "something the learner has just said, and this turn does not carry one — it may be a "
    "scheduled job, a background task, or a turn that has already moved on. Nothing was "
    "started. Offer the exercise the next time they write."
)

QUOTE_MISMATCH = (
    "The words you quoted are not in the learner's current message, so the Learning Studio "
    "cannot confirm they asked for this. Quote what they actually wrote, or ask them. "
    "Nothing was started."
)

ALREADY_USED = (
    "That message has already opened an exercise, and the earlier one is no longer "
    "available. Ask the learner again before starting a new one. Nothing was started."
)

UNUSABLE_QUOTE = (
    "The quotation is too short to confirm anything. Quote a few words the learner "
    "actually wrote. Nothing was started."
)


@dataclass(frozen=True)
class Decision:
    """What the policy concluded, and what the caller may therefore do."""

    initiation: str
    #: False when the caller may only return a launch that already exists.
    may_create: bool
    #: The trusted message this authority came from. The caller spends it, and
    #: only after the launch has actually committed.
    key: EvidenceKey
    #: Said in the response. Written to describe what was *proved*, not to
    #: repeat the model's own assertion back as a finding.
    basis: str


def decide(
    *,
    profile: str,
    initiation: str,
    learner_confirmed: bool,
    learner_quote: str | None,
    store: EvidenceStore | None = None,
) -> Decision:
    """Apply the policy, or raise :class:`ConsentRequired`.

    Nothing is spent here. The caller spends the decision's key with
    :func:`spend` once a launch has actually been delivered, so a failure
    further down — no runtime, no tunnel, no message — leaves the learner's
    words intact and the next attempt does not need a second ask.
    """
    # `is None`, never `or`. An `EvidenceStore` defines `__len__`, so an empty
    # one is falsy — and `store or STORE` therefore fell through to the global
    # store the moment the injected one was emptied by spending. The visible
    # symptom was a message reading as never-used immediately after being used.
    evidence = STORE if store is None else store

    if initiation not in INITIATIONS:
        raise ConsentRequired(NOT_CONFIRMED, reason="initiation_unknown")
    if initiation == AGENT_SUGGESTION and not learner_confirmed:
        raise ConsentRequired(NOT_CONFIRMED, reason="suggestion_not_confirmed")

    from .evidence import current_key

    key = current_key(profile)
    state = evidence.state(key, learner_quote or "")

    if state == "matched":
        return Decision(
            initiation=initiation,
            may_create=True,
            key=key,
            basis=_BASIS[initiation],
        )
    if state == "spent":
        # The message is real and was theirs; it has simply already been used.
        # A repeat may report the launch it opened, and may not open another.
        return Decision(
            initiation=initiation,
            may_create=False,
            key=key,
            basis="Reporting the exercise that message already opened.",
        )
    if state == "mismatched":
        raise ConsentRequired(QUOTE_MISMATCH, reason="quote_not_in_current_message")
    if state == "unusable":
        raise ConsentRequired(UNUSABLE_QUOTE, reason="quote_unusable")
    raise ConsentRequired(NO_EVIDENCE, reason="no_current_learner_message")


#: What the response is allowed to claim, per initiation.
#:
#: Both sentences describe a *check that passed*, not a state of mind. The
#: earlier wording said "the learner agreed", which repeated the model's own
#: assertion back as though this plugin had established it.
_BASIS = {
    LEARNER_REQUEST: (
        "The words you quoted are in the learner's current message, and you read them as a "
        "request to practise."
    ),
    AGENT_SUGGESTION: (
        "The words you quoted are in the learner's current message, and you read them as "
        "agreement to the exercise you suggested."
    ),
}


def spend(decision: Decision, *, store: EvidenceStore | None = None) -> bool:
    """Consume the message's authority. Returns True for the caller that won.

    Atomic, so two launches racing on one message do not both commit. Called
    only after delivery has succeeded — see
    :func:`learning_studio.launch.launch_experience`.
    """
    evidence = STORE if store is None else store
    return evidence.spend(decision.key)
