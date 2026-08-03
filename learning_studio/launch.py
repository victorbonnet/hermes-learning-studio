"""The launch transaction: nine steps, and one place where it commits.

Opening an exercise is the only thing this plugin does that reaches outside the
machine. It creates a temporary public address and sends a person a message, so
it is written as a transaction with a single commit point and a rollback that
touches nothing it did not create.

The order is the design
-----------------------

1. **Who is asking**, from Hermes' session context. Never from an argument.
2. **Which exercise**, by opaque id, checked in SQL against that learner.
3. **Where would the message go?** — :mod:`learning_studio.destination`,
   derived from the authenticated private chat and the profile's own
   allowlist. Resolved *before* anything is started, because a launch that
   could never be delivered should not start a server first — and before
   consent, so somebody in a group is told they are in a group.
4. **May this happen at all?** — :mod:`learning_studio.consent`, which checks
   the quotation against the message the platform actually delivered.
5. **A runtime**, started or reused, under the profile lock.
6. **A public address**, which the runtime's tunnel has already validated and
   which is validated again on the way in.
7. **A grant**: expiring, and bound to profile, generation, Telegram account,
   learner and experience.
8. **The button.**
9. **Commit** — and only here. Everything before this point is undone if
   anything after it fails.

Rollback
--------

Two rules, and the second is the one that matters:

- The grant this call created is revoked, which also ends any session opened
  under it. A learner is never left holding an entrance to an exercise the
  agent has been told was not delivered.
- The runtime is stopped **only if this call started it**. A reused runtime may
  be serving somebody else's session, and tearing it down because a second
  launch failed to send a message would make one learner's failure another
  learner's interruption.

What never travels
------------------

The tunnel address is used to build one button and is then dropped. It is not
in the return value, not in the status payload, not in a log line, and not in a
memory candidate. Neither is the bot token, the chat id, the Telegram user id,
the control secret, or a session token. What the agent gets back is: it worked,
here is an opaque launch id, and here is what is *not* being recorded.
"""

from __future__ import annotations

import logging
from typing import Any

from . import consent as consent_policy
from . import service
from .config import LearningStudioConfig, load_config
from .identity import Principal
from .runtime import manager, ownership, supervisor
from .runtime.errors import (
    CLEANUP_INDETERMINATE,
    DELIVERY_INDETERMINATE,
    TUNNEL_FAILED,
    RuntimeUnavailable,
)
from .runtime.state import ProfileLock

logger = logging.getLogger(__name__)

#: Said when the runtime came up but published no usable public address.
NO_PUBLIC_ADDRESS = TUNNEL_FAILED


def launch_experience(
    *,
    principal: Principal,
    experience_id: str,
    initiation: str,
    learner_quote: str,
    learner_confirmed: bool = False,
    config: LearningStudioConfig | None = None,
    deliver=None,
    evidence=None,
) -> dict[str, Any]:
    """Open a prepared exercise for the learner who asked for it.

    ``deliver`` is the seam where a Telegram message is actually sent. It is a
    parameter so that every test in this repository exercises the whole
    transaction — runtime, grant, rollback, reuse — without a network, and so
    that the one function that holds a bot token is small enough to read.
    """
    settings = config or load_config()
    send = deliver or _default_deliver

    # (1) and (2): identity is already resolved; ownership is checked in SQL,
    # and a missing experience and somebody else's are the same refusal.
    bundle = service.delivery_bundle(
        principal=principal, experience_id=str(experience_id), config=settings
    )
    experience = bundle.experience

    # (3) The destination, before anything else that could fail. It is derived
    # entirely from trusted session context, it is cheap, and putting it first
    # means a learner in a group chat is told *that* rather than being told
    # something about consent — which would be true, but useless to them.
    from .destination import resolve_destination

    destination = resolve_destination(principal=principal, config=settings)

    # (4) Consent, before anything exists to roll back. The quotation is
    # checked against the message the platform actually delivered — see
    # `learning_studio.evidence` — so this step can fail on a launch the model
    # was certain about, which is the entire reason it is here.
    decision = consent_policy.decide(
        profile=principal.profile,
        initiation=initiation,
        learner_confirmed=learner_confirmed,
        learner_quote=learner_quote,
        store=evidence,
    )

    manager.require_prerequisites(settings)

    # (5) Claim the learner's message *now*, before anything exists and before
    # anything is sent. Two calls racing on one sentence used to get this far
    # together, both create a grant, and both deliver a button — the loser of
    # the eventual spend was simply ignored, and the learner got two messages.
    #
    # A repeat that is only allowed to report an existing launch does not
    # reserve: it is not going to create anything, and taking the claim would
    # stop the launch it is reporting on from ever committing.
    reserved = decision.may_create and consent_policy.reserve(decision, store=evidence)
    if decision.may_create and not reserved:
        raise consent_policy.ConsentRequired(
            consent_policy.ALREADY_USED, reason="confirmation_already_used"
        )

    try:
        with ProfileLock():
            handle = supervisor.ensure_running(settings)
            started_here = handle.started
            try:
                return _launch_within(
                    settings=settings,
                    handle=handle,
                    destination=destination,
                    bundle=bundle,
                    experience=experience,
                    principal=principal,
                    decision=decision,
                    evidence=evidence,
                    send=send,
                )
            except BaseException:
                if started_here:
                    # Only what this call created. A reused runtime is somebody
                    # else's session as far as this function knows.
                    supervisor.stop(settings)
                raise
    except _MayHaveBeenSent as marker:
        # A message may exist in the learner's chat. The claim is *kept*, so a
        # retry cannot put a second button beside a first one nobody can see.
        # The marker is plumbing; what an agent sees is the original failure.
        raise marker.original from None
    except BaseException:
        # Everything else failed before the sender was ever called, or failed
        # in a way that proves no request reached Telegram. Hand the claim back
        # so the learner is not made to repeat themselves for nothing.
        #
        # Only ours. A reuse-only call took no reservation, and releasing on its
        # way out would hand back the claim held by the launch it was reporting
        # on — which is how a "no second button" rule turns into two buttons.
        if reserved:
            consent_policy.release(decision, store=evidence)
        raise


def _proves_nothing_was_sent(exc: BaseException) -> bool:
    """Whether this delivery failure is evidence that no message exists."""
    from .telegram_launch import proves_nothing_was_sent

    reason = getattr(exc, "reason", "")
    return bool(reason) and proves_nothing_was_sent(reason)


class _MayHaveBeenSent(Exception):
    """Marker: this failure happened where a message could already exist.

    The default is the safe one — a failure is undelivered unless it is wrapped
    in this — so a new early-exit added to the launch path releases the claim by
    omission rather than silently authorising a second button.
    """

    def __init__(self, original: BaseException) -> None:
        super().__init__(str(original))
        self.original = original


def _launch_within(
    *,
    settings: LearningStudioConfig,
    handle,
    destination,
    bundle,
    experience: dict[str, Any],
    principal: Principal,
    decision: consent_policy.Decision,
    evidence,
    send,
) -> dict[str, Any]:
    """Steps 5 to 9, as a transaction with one commit point.

    The states, in order, and what each one means for the learner:

    ``pending``  a grant exists and admits nobody. The selector has to exist
                 before the message can carry it, so this is the gap between
                 "we have decided to launch" and "they can open it".
    ``delivered`` the button reached Telegram.
    ``open``     the grant is activated and the learner's message is spent.
                 This is the commit, and the only state a success is reported
                 from.
    ``indeterminate`` the message went out and the commit did not finish, or a
                 rollback could not be confirmed. Reported as unknown, because
                 it is.

    The previous version created a grant that was live from the moment it
    existed, sent the message, and then spent consent — so a failure after the
    send left a usable entrance while the caller was told the launch had failed.
    """
    public_url = handle.public_url
    if not public_url:
        raise RuntimeUnavailable(NO_PUBLIC_ADDRESS, reason="tunnel_not_ready")

    granted = ownership.call(
        handle.record,
        ownership.GRANT_PATH,
        {
            "telegram_user_id": destination.telegram_user_id,
            "learner_id": bundle.learner_id,
            "experience_id": str(experience["experience_id"]),
            # When the learner's message has already been spent, this call may
            # report the launch that exists and may not become a second one.
            # Enforced in the runtime, so nothing is created and then undone.
            "reuse_only": not decision.may_create,
        },
    )

    if not granted.get("created") and not granted.get("reused"):
        raise consent_policy.ConsentRequired(
            consent_policy.ALREADY_USED, reason="confirmation_already_used"
        )

    if granted.get("reused"):
        # A repeat. The learner already has a button for this exercise and a
        # second message would be noise — or worse, a second thing to tap after
        # the first one stopped working.
        return _result(
            experience=experience,
            granted=granted,
            decision=decision,
            delivered=False,
            reused=True,
        )

    launch_id = str(granted["launch_id"])
    try:
        send(
            destination=destination,
            url=public_url,
            label=settings.launch_button_label,
            title=str(experience["title"]),
            # The selector the button carries. Without it the page has nothing
            # to open: an inline web_app button produces no start_param, which
            # is what made the first version of this launch nothing at all.
            launch_id=launch_id,
        )
    except BaseException as exc:
        # Whether the learner's sentence may be used again turns on whether a
        # message could exist, and the sender is what knows: an absent token or
        # a refused request proves none does, while a connection that dropped
        # mid-request proves nothing at all.
        provable = _proves_nothing_was_sent(exc)

        # The pending grant must go either way. If it will not go, that is
        # reported as *indeterminate* rather than swallowed: the original
        # failure is still what the agent needs, but "nothing was saved" would
        # be a claim this call cannot make.
        if not _revoke(handle, launch_id):
            failure = RuntimeUnavailable(CLEANUP_INDETERMINATE, reason="rollback_indeterminate")
            raise (failure if provable else _MayHaveBeenSent(failure)) from None
        if provable:
            raise exc
        raise _MayHaveBeenSent(exc) from None

    # ── Past this line a message exists in the learner's chat. ──────────
    #
    # Nothing below may hand the claim on their sentence back, whatever goes
    # wrong, because every path here is one where a button has already been
    # delivered. So the whole region is wrapped: any failure at all leaves as
    # `_MayHaveBeenSent`, rather than only the failures somebody remembered to
    # mark. The previous version marked two of them and let a third — an
    # exception out of `commit` itself — fall through to the release, which
    # allowed a retry to send and activate a second button.
    try:
        # Commit, in two steps: the grant starts admitting its learner, and
        # then the reservation on their message becomes a spend.
        if not _activate(handle, launch_id):
            with _suppressed("revoking a grant that could not be committed"):
                _revoke(handle, launch_id)
            raise RuntimeUnavailable(DELIVERY_INDETERMINATE, reason="commit_indeterminate")

        # The **return value matters.** False means this call no longer held
        # the reservation it was about to spend — somebody else resolved it —
        # so the launch is not in the state a success describes. It used to be
        # discarded, and the caller was told the exercise was open.
        if not consent_policy.commit(decision, store=evidence):
            with _suppressed("revoking a launch whose consent could not be spent"):
                _revoke(handle, launch_id)
            raise RuntimeUnavailable(DELIVERY_INDETERMINATE, reason="consent_commit_lost")
    except _MayHaveBeenSent:
        raise
    except BaseException as exc:
        raise _MayHaveBeenSent(exc) from None

    return _result(
        experience=experience, granted=granted, decision=decision, delivered=True, reused=False
    )


def _activate(handle, launch_id: str) -> bool:
    """Commit the grant, or report that it could not be committed."""
    try:
        reply = ownership.call(
            handle.record, ownership.GRANT_ACTIVATE_PATH, {"launch_id": launch_id}
        )
    except Exception as exc:
        logger.warning("a launch could not be committed: %s", type(exc).__name__)
        return False
    return bool(reply.get("activated"))


def _revoke(handle, launch_id: str) -> bool:
    """Undo a grant, or report that the undo could not be confirmed."""
    try:
        reply = ownership.call(handle.record, ownership.GRANT_REVOKE_PATH, {"launch_id": launch_id})
    except Exception as exc:
        logger.warning("a launch could not be rolled back: %s", type(exc).__name__)
        return False
    return bool(reply.get("revoked"))


def _result(
    *,
    experience: dict[str, Any],
    granted: dict[str, Any],
    decision: consent_policy.Decision,
    delivered: bool,
    reused: bool,
) -> dict[str, Any]:
    """The agent-facing answer. Built field by field; nothing passed through."""
    return {
        "ok": True,
        "launched": True,
        "launch_id": granted.get("launch_id"),
        "experience_id": experience["experience_id"],
        "title": experience["title"],
        "component_count": len(experience["components"]),
        "button_delivered": delivered,
        "reused_existing_launch": reused,
        "basis": decision.basis,
        "expires_in_seconds": granted.get("expires_in_seconds"),
        "delivery": (
            "A button to open the exercise has been sent to the learner in this chat. Tell "
            "them it is there; do not repeat the link, because there is no link to repeat."
            if delivered
            else "The learner already has a button for this exercise; nothing new was sent."
        ),
        **_honest_scoring(),
    }


def _honest_scoring() -> dict[str, Any]:
    return {
        "scored": False,
        "attempts_stored": False,
        "notice": (
            "Nothing about how the learner does will be stored: no attempt, score, mastery, "
            "or progress record exists in this release. Ask them how it went, and use "
            "learning_studio_results only for whether they opened and finished it."
        ),
    }


class _suppressed:
    """``contextlib.suppress`` that says what it swallowed, at DEBUG.

    Used on the rollback path, where a second failure must not replace the
    first: the caller is already raising the error the agent needs to see.
    """

    def __init__(self, what: str) -> None:
        self.what = what

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback) -> bool:
        if value is not None:
            logger.debug("%s failed: %s", self.what, type(value).__name__)
        return True


def _default_deliver(**kwargs: Any) -> None:
    """Send the button, through the one module that holds a bot token.

    Imported here rather than at this module's scope so that the single file
    able to reach a remote host is loaded only on the path that actually sends
    something — reading a launch's ownership and consent logic does not need to
    drag in a network client.
    """
    from .telegram_launch import deliver_web_app_button

    deliver_web_app_button(**kwargs)


def launch_results(
    *,
    principal: Principal,
    experience_id: str,
    config: LearningStudioConfig | None = None,
) -> dict[str, Any]:
    """What happened to a launch, with nothing invented.

    The honest answer in this release is short, and the shape of the response
    says so in three places rather than one, because "how did they do?" is the
    question a reader most easily assumes has been answered.

    What is actually known: whether an exercise was opened, which component the
    learner reached, how many they answered, and whether they finished. Those
    are facts the runtime holds while a session is alive. What is *not* known,
    and is stated as not known: any mark, score, mastery estimate, durable
    attempt, or review schedule. None of those exists in this release, and a
    field reporting one would be a fabrication with a plausible name.

    A learner's answers are never returned either. How far somebody got is
    progress; what they wrote is their work, and an agent that wants to discuss
    it should ask them.
    """
    settings = config or load_config()

    # Ownership first, in SQL, against this principal. A learner asking about
    # somebody else's exercise gets the same answer as one asking about an
    # exercise that does not exist.
    bundle = service.delivery_bundle(
        principal=principal, experience_id=str(experience_id), config=settings
    )
    experience = bundle.experience

    handle = supervisor.current(settings)
    if handle is None:
        # Not "they did not open it" — *this cannot be known*. The runtime that
        # would have held that answer is gone, and reporting `opened: false`
        # here would state as a finding something that was merely unobservable.
        return _unknown(
            experience,
            state="not_running",
            message=(
                "No Learning Studio runtime is open for this profile, so whether the learner "
                "ever opened this exercise cannot be determined. Nothing was recorded while "
                "it was running either way."
            ),
        )

    try:
        progress = ownership.call(
            handle.record,
            ownership.LAUNCH_PATH,
            {
                "telegram_user_id": principal.user_id,
                "experience_id": str(experience["experience_id"]),
            },
        )
    except ownership.ControlError:
        return _unknown(
            experience,
            state="unavailable",
            message=(
                "The Learning Studio runtime did not answer, so what happened to this "
                "exercise cannot be determined right now."
            ),
        )

    if not progress.get("found"):
        return _unknown(
            experience,
            state="not_launched",
            message=(
                "The current Learning Studio runtime has no record of this exercise being "
                "opened for this learner. An earlier runtime may have; that is not something "
                "this can see."
            ),
        )

    return {
        "ok": True,
        "experience_id": experience["experience_id"],
        "title": experience["title"],
        "availability": "known",
        "state": progress.get("state"),
        "opened": bool(progress.get("opened")),
        "position": progress.get("position"),
        # From the stored exercise, not from the session. A launch nobody has
        # opened yet has no session, and reporting "0 of 0" for an exercise
        # with five questions in it reads as an answer rather than as an
        # absence. How many there are is known; how far they got is not.
        "component_count": len(experience["components"]),
        "answered": progress.get("answered"),
        "completed": bool(progress.get("completed")),
        "responses_returned": False,
        # Bounded, selective, and empty on purpose. See the docstring: finishing
        # one exercise is an event, not a durable fact about a person, and this
        # release observes nothing else. Anything worth remembering comes from
        # the conversation and goes through `learning_studio_save_context`.
        "memory_candidates": [],
        "memory_candidates_note": (
            "No durable memory candidate is proposed from running an exercise. Completing "
            "one is an event rather than a fact that stays true, and no mark or attempt is "
            "recorded to draw a conclusion from. Propose durable facts through "
            "learning_studio_save_context, from what the learner actually told you."
        ),
        **_honest_scoring(),
    }


def _unknown(experience: dict[str, Any], *, state: str, message: str) -> dict[str, Any]:
    """A result that says "I cannot tell", and says which kind of cannot.

    ``opened`` is ``None`` rather than ``False`` throughout. The distinction is
    the whole point of this shape: ``False`` is a finding — the learner did not
    open it — and this plugin is only entitled to that when a runtime told it
    so. Everywhere else the honest answer is that the evidence is not available,
    and an agent that reads ``False`` as "they ignored it" would be repeating a
    conclusion nobody reached.
    """
    return {
        "ok": True,
        "experience_id": experience["experience_id"],
        "title": experience["title"],
        "availability": "unavailable",
        "state": state,
        "opened": None,
        "position": None,
        "component_count": len(experience["components"]),
        "answered": None,
        "completed": None,
        "responses_returned": False,
        "memory_candidates": [],
        "message": message,
        **_honest_scoring(),
    }
